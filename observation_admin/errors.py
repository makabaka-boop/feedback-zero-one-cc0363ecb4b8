"""Domain errors.

Every rejected operation raises :class:`AdminError`; the caller receives a
stable ``code`` and nothing has been persisted.
"""


class AdminError(Exception):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail) if detail else code)

    def to_dict(self):
        out = {"error": self.code}
        if self.detail is not None:
            out["detail"] = self.detail
        return out
