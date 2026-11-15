"""Incremental UTF-8 JSON reads over immutable SQLite blobs or binary files."""

import codecs
import json
import re


class SnapshotJSONError(ValueError):
    """Malformed or oversized metadata; messages never include stored payloads."""


SPECIAL_STRING = re.compile(r'["\\\x00-\x1f]')
NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


class SnapshotJSON:
    def __init__(self, stream, *, chunk_bytes=65536, max_value_bytes=262144):
        if not 1 <= chunk_bytes <= 65536 or max_value_bytes < 1:
            raise ValueError("Invalid snapshot reader bounds")
        self.stream = stream
        self.chunk_bytes = chunk_bytes
        self.max_value_bytes = max_value_bytes
        self._utf8 = codecs.getincrementaldecoder("utf-8")()
        self._decoder = json.JSONDecoder(parse_constant=self._invalid_constant)
        self._buffer = ""
        self._position = 0
        self._base = stream.tell()
        self._eof = False
        self.peak_buffer_bytes = 0

    @staticmethod
    def _invalid_constant(_):
        raise SnapshotJSONError("Invalid JSON constant")

    @property
    def offset(self):
        return self._base + len(self._buffer[: self._position].encode("utf-8"))

    def _fill(self):
        if self._eof:
            return False
        self._base = self.offset
        self._buffer = self._buffer[self._position :]
        self._position = 0
        raw = self.stream.read(self.chunk_bytes)
        try:
            self._buffer += self._utf8.decode(raw, final=not raw)
        except UnicodeError:
            raise SnapshotJSONError("Invalid snapshot UTF-8") from None
        self._eof = not raw
        self.peak_buffer_bytes = max(self.peak_buffer_bytes, len(self._buffer.encode("utf-8")))
        return bool(raw)

    def _char(self):
        while self._position >= len(self._buffer):
            if not self._fill() and self._position >= len(self._buffer):
                return None
        return self._buffer[self._position]

    def peek(self):
        while (char := self._char()) is not None and char in " \r\n\t":
            self._position += 1
        return char

    def take(self, expected):
        if self.peek() != expected:
            raise SnapshotJSONError("Invalid JSON structure")
        self._position += 1

    def value(self):
        first = self.peek()
        if first is None:
            raise SnapshotJSONError("Incomplete JSON value")
        if first in "-0123456789":
            token = []
            while (char := self._char()) is not None and char in "0123456789.eE+-":
                token.append(char)
                self._position += 1
                if len(token) > self.max_value_bytes:
                    raise SnapshotJSONError("Snapshot value limit exceeded")
            raw = "".join(token)
            if not NUMBER.fullmatch(raw):
                raise SnapshotJSONError("Invalid JSON number")
            return json.loads(raw)
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buffer, self._position)
            except (json.JSONDecodeError, RecursionError):
                if len(self._buffer[self._position :].encode("utf-8")) > self.max_value_bytes:
                    raise SnapshotJSONError("Snapshot value limit exceeded") from None
                if not self._fill():
                    raise SnapshotJSONError("Invalid or incomplete JSON value") from None
                continue
            if len(self._buffer[self._position : end].encode("utf-8")) > self.max_value_bytes:
                raise SnapshotJSONError("Snapshot value limit exceeded")
            self._position = end
            return value

    def object_keys(self):
        self.take("{")
        if self.peek() == "}":
            self.take("}")
            return
        while True:
            key = self.value()
            if not isinstance(key, str):
                raise SnapshotJSONError("Invalid JSON object key")
            self.take(":")
            yield key
            if self.peek() == "}":
                self.take("}")
                return
            self.take(",")

    def array_offsets(self):
        self.take("[")
        if self.peek() == "]":
            self.take("]")
            return
        while True:
            self.peek()
            offset = self.offset
            yield offset, self.value()
            if self.peek() == "]":
                self.take("]")
                return
            self.take(",")

    def _skip_string(self):
        self.take('"')
        while True:
            if self._char() is None:
                raise SnapshotJSONError("Incomplete JSON string")
            match = SPECIAL_STRING.search(self._buffer, self._position)
            if match is None:
                self._position = len(self._buffer)
                continue
            self._position = match.end()
            char = match[0]
            if char == '"':
                return
            if char != "\\":
                raise SnapshotJSONError("Invalid JSON string")
            escaped = self._char()
            if escaped is None or escaped not in '"\\/bfnrtu':
                raise SnapshotJSONError("Invalid JSON escape")
            self._position += 1
            if escaped == "u":
                for _ in range(4):
                    digit = self._char()
                    if digit is None or digit not in "0123456789abcdefABCDEF":
                        raise SnapshotJSONError("Invalid Unicode escape")
                    self._position += 1

    def skip(self, _depth=0):
        if _depth > 64:
            raise SnapshotJSONError("Snapshot nesting limit exceeded")
        char = self.peek()
        if char == "{":
            for _ in self.object_keys():
                self.skip(_depth + 1)
        elif char == "[":
            self.take("[")
            if self.peek() != "]":
                while True:
                    self.skip(_depth + 1)
                    if self.peek() == "]":
                        break
                    self.take(",")
            self.take("]")
        elif char == '"':
            self._skip_string()
        else:
            self.value()

    def finish(self):
        if self.peek() is not None:
            raise SnapshotJSONError("Trailing snapshot data")
