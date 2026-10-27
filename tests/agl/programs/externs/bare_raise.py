from agl import AglException, Problem


def relay(f):
    f()


def originate():
    raise AglException(Problem(detail="companion", message="original"))
