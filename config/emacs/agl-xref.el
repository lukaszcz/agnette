;;; agl-xref.el --- Go-to-definition for agl-mode -*- lexical-binding: t; -*-

;; Package-Requires: ((emacs "27.1"))

;;; Commentary:

;; An xref backend for AgL, backed by `agm dev definition': the compiler
;; resolves the name at point with its own scope and type information, so
;; M-. jumps exactly where the program binds the name, and M-, returns.
;;
;; The buffer's text is sent on stdin, so unsaved edits count, while the
;; file's real path anchors module roots and imports.  The lookup is by
;; position, not by name: an identifier typed at the xref prompt has no
;; position and finds nothing.

;;; Code:

(require 'xref)
(require 'subr-x)

(defcustom agl-definition-command '("agm" "dev" "definition")
  "Command vector that finds the declaration of an AgL name.

The backend appends `--stdin FILE LINE COLUMN', so any flags belong here."
  :type '(repeat string)
  :group 'agl)

(defconst agl-xref--location-re "^\\(.+?\\):\\([0-9]+\\):\\([0-9]+\\): \\(.*\\)$"
  "Regexp matching one `PATH:LINE:COLUMN: TEXT' line of `agm dev definition'.")

(defun agl-xref-parse (output)
  "Return the xref items `agm dev definition' printed in OUTPUT.

Each line names a declaration with a 1-based column; xref columns are
0-based.  Other lines are ignored."
  (let ((items nil))
    (dolist (line (split-string (or output "") "\n"))
      (when (string-match agl-xref--location-re line)
        (push (xref-make (match-string 4 line)
                         (xref-make-file-location
                          (match-string 1 line)
                          (string-to-number (match-string 2 line))
                          (1- (string-to-number (match-string 3 line)))))
              items)))
    (nreverse items)))

(defun agl-xref--command (file position)
  "Return the command finding the declaration at POSITION of FILE's buffer."
  (save-excursion
    (save-restriction
      (widen)
      (goto-char position)
      (append agl-definition-command
              (list "--stdin" file
                    (number-to-string (line-number-at-pos))
                    (number-to-string (1+ (- position (line-beginning-position)))))))))

(defun agl-xref--definitions (position)
  "Return xref items for the declaration of the name at POSITION.

Signals a `user-error' with the command's error output when it finds
nothing and explains why."
  (unless (executable-find (car agl-definition-command))
    (user-error "Cannot find %s" (car agl-definition-command)))
  (let ((command (agl-xref--command (buffer-file-name) position))
        (errors (make-temp-file "agl-xref-")))
    (unwind-protect
        (let* ((output (with-output-to-string
                         (save-restriction
                           (widen)
                           (apply #'call-process-region (point-min) (point-max)
                                  (car command) nil
                                  (list standard-output errors) nil
                                  (cdr command)))))
               (items (agl-xref-parse output))
               (reason (with-temp-buffer
                         (insert-file-contents errors)
                         (string-trim (buffer-string)))))
          (when (and (null items) (not (string-empty-p reason)))
            (user-error "%s" reason))
          items)
      (delete-file errors))))

(defun agl-xref-backend ()
  "Return the AgL xref backend for a buffer visiting a file."
  (and (buffer-file-name) 'agl))

(cl-defmethod xref-backend-identifier-at-point ((_backend (eql agl)))
  "Return the text at point, remembering point for the lookup."
  (unless (eobp)
    (propertize (or (thing-at-point 'symbol t) (string (char-after)))
                'agl-xref-position (point))))

(cl-defmethod xref-backend-identifier-completion-table ((_backend (eql agl)))
  "AgL lookups are positional, so there is nothing to complete."
  nil)

(cl-defmethod xref-backend-definitions ((_backend (eql agl)) identifier)
  "Return the declarations of IDENTIFIER, when it was taken at point."
  (let ((position (get-text-property 0 'agl-xref-position identifier)))
    (and position (agl-xref--definitions position))))

(defun agl-xref-setup ()
  "Register the AgL xref backend in this buffer."
  (add-hook 'xref-backend-functions #'agl-xref-backend nil t))

(provide 'agl-xref)
;;; agl-xref.el ends here
