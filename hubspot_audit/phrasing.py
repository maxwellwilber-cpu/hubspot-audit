"""Small grammar helpers for finding titles.

A report that says "1 contacts have no owner" tells the reader the tool was
assembled carelessly, and they are then entitled to wonder what else was. The
numbers are the product here, so the sentences around them have to hold up.
"""


def n_of(count, singular, plural=None):
    """'1 contact' / '2 contacts'."""
    word = singular if count == 1 else (plural or singular + "s")
    return "%d %s" % (count, word)


def have(count):
    return "has" if count == 1 else "have"


def is_are(count):
    return "is" if count == 1 else "are"


def do_does(count):
    return "does" if count == 1 else "do"


def this_these(count):
    return "This" if count == 1 else "These"
