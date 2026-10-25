from agl import Handle, Token


def mint_int(id, label):
    return Handle(id=id, label=label)


def mint_text(id, label):
    return Handle(id=id, label=label)


def serial_of(h):
    return h.id * 100


def echo(h):
    return h


def mint_token(serial):
    return Token(serial=serial)


def bump(token):
    return Token(serial=token.serial + 1)


def mint_bogus():
    return object()
