from agl import AglException, Handle, transfer


def copy(value):
    return transfer.loads(transfer.dumps(value))


def mint(id):
    return Handle(id=id)


def copy_raise(f):
    try:
        return f()
    except AglException as error:
        raise transfer.loads(transfer.dumps(error)) from None
