;;; agl-xref-tests.el --- ERT tests for the agl-mode xref backend -*- lexical-binding: t; -*-

;;; Commentary:

;; Tests the AgL go-to-definition backend.  The suite never runs `agm':
;; `call-process-region' is replaced by a stub that records the command
;; and replays canned output.

;;; Code:

(require 'ert)
(require 'cl-lib)
(require 'agl-mode)
(require 'agl-xref)

(defmacro agl-xref--with-buffer (text &rest body)
  "Evaluate BODY in an `agl-mode' buffer holding TEXT and visiting a file."
  (declare (indent 1))
  `(let ((file (make-temp-file "agl-xref-" nil ".agl"))
         (agl-flymake-enable nil))
     (unwind-protect
         (with-temp-buffer
           (setq buffer-file-name file)
           (agl-mode)
           (insert ,text)
           ,@body)
       (delete-file file))))

(defmacro agl-xref--with-agm (stdout stderr calls &rest body)
  "Evaluate BODY with `agm' printing STDOUT and STDERR, pushing calls onto CALLS.

Each call is recorded as (COMMAND . STDIN)."
  (declare (indent 3))
  `(cl-letf (((symbol-function 'executable-find) (lambda (_) "/bin/agm"))
             ((symbol-function 'call-process-region)
              (lambda (start end program _delete destination _display &rest args)
                (push (cons (cons program args) (buffer-substring-no-properties start end))
                      ,calls)
                (princ ,stdout (car destination))
                (with-temp-file (cadr destination) (insert ,stderr))
                (if (string-empty-p ,stdout) 1 0))))
     ,@body))

(defun agl-xref--location (item)
  "Return ITEM's (FILE LINE COLUMN SUMMARY)."
  (let ((location (xref-item-location item)))
    (list (xref-file-location-file location)
          (xref-file-location-line location)
          (xref-file-location-column location)
          (xref-item-summary item))))

(ert-deftest agl-xref-parses-declaration-lines ()
  (should (equal (mapcar #'agl-xref--location
                         (agl-xref-parse "/a/lib.agl:3:5: def f() = 1\nnoise\n/b.agl:1:1: x: y\n"))
                 '(("/a/lib.agl" 3 4 "def f() = 1") ("/b.agl" 1 0 "x: y")))))

(ert-deftest agl-xref-sends-the-whole-buffer-and-the-position-of-point ()
  (agl-xref--with-buffer "def f() = 1\n\nlet x = f()\n"
    (goto-char (point-max))
    (search-backward "f()")
    (narrow-to-region (line-beginning-position) (point-max))
    (let ((calls nil))
      (agl-xref--with-agm "/a/lib.agl:1:5: def f() = 1\n" "" calls
        (let ((identifier (xref-backend-identifier-at-point 'agl)))
          (should (equal identifier "f"))
          (should (equal (mapcar #'agl-xref--location
                                 (xref-backend-definitions 'agl identifier))
                         '(("/a/lib.agl" 1 4 "def f() = 1"))))))
      (should (equal calls
                     (list (cons (list "agm" "dev" "definition" "--stdin"
                                       buffer-file-name "3" "9")
                                 "def f() = 1\n\nlet x = f()\n")))))))

(ert-deftest agl-xref-reports-why-nothing-was-found ()
  (agl-xref--with-buffer "let x = y\n"
    (let ((calls nil))
      (agl-xref--with-agm "" "main.agl:1:9: error: unknown name y\n" calls
        (should-error (agl-xref--definitions 9) :type 'user-error))
      (agl-xref--with-agm "" "" calls
        (should-not (agl-xref--definitions 1))))))

(ert-deftest agl-xref-honours-a-customized-command ()
  (agl-xref--with-buffer "x\n"
    (let ((agl-definition-command '("agm" "dev" "definition" "-I" "lib")))
      (should (equal (agl-xref--command buffer-file-name 1)
                     (list "agm" "dev" "definition" "-I" "lib" "--stdin"
                           buffer-file-name "1" "1"))))))

(ert-deftest agl-xref-needs-a-position-and-agm ()
  (agl-xref--with-buffer "x"
    (goto-char (point-max))
    (should-not (xref-backend-identifier-at-point 'agl))
    (should-not (xref-backend-definitions 'agl "x"))
    (should-not (xref-backend-identifier-completion-table 'agl))
    (cl-letf (((symbol-function 'executable-find) #'ignore))
      (should-error (agl-xref--definitions 1) :type 'user-error))))

(ert-deftest agl-mode-registers-the-xref-backend-for-files ()
  (agl-xref--with-buffer ""
    (should (eq (xref-find-backend) 'agl)))
  (with-temp-buffer
    (agl-mode)
    (should-not (agl-xref-backend))))

(ert-deftest agl-xref-takes-an-operator-or-a-character-at-point ()
  (agl-xref--with-buffer "1 +++ (2)"
    (goto-char 7)
    (should (equal (xref-backend-identifier-at-point 'agl) "("))
    (goto-char 3)
    (should (equal (xref-backend-identifier-at-point 'agl) "+++"))))

(provide 'agl-xref-tests)
;;; agl-xref-tests.el ends here
