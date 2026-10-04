import time


# EXPLAIN(human) c01
#
def retry(fn, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            time.sleep(2 ** i)
    raise RuntimeError("out of attempts")
