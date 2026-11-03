"""Invalidation-aware in-process reservations shared by all media producers."""


class StorageReservations(dict):
    def __init__(self, changed):
        super().__init__()
        self.changed = changed

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.changed()

    def pop(self, key, *default):
        present = key in self
        value = super().pop(key, *default)
        if present:
            self.changed()
        return value

    def clear(self):
        super().clear()
        self.changed()
