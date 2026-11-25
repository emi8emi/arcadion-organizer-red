"""A thin Postgres connection (psycopg 3) that keeps the bot's database code simple.

It gives the rest of the bot the handful of things it used from sqlite3:

* `conn.execute(sql, params)` with `?` placeholders (translated to psycopg's `%s`), returning a cursor.
* Rows you can read by name or position: `row["id"]`, `row[0]`, `dict(row)`, `tuple(row)`.
* `with conn:` is one transaction: commit when the block ends, roll back on an exception. Blocks may nest
  (an inner block is a savepoint). Outside a block every statement commits by itself, so the bot never sits
  in an open transaction, which would block the dashboard's migrations.
* A lost connection (database restarted, idle timeout, network blip) is re-opened once, for a statement that
  is not inside a `with` block.

Not here: anything SQLite-specific (`lastrowid`, `INSERT OR ...`, PRAGMA). Write Postgres SQL.
"""
import functools
import logging

import psycopg

log = logging.getLogger("raidbot")


class Row:
    """A result row readable by column name or by position, like sqlite3.Row."""
    __slots__ = ("_names", "_values")

    def __init__(self, names, values):
        self._names, self._values = names, values

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._names.index(key)]
            except ValueError:
                raise KeyError(key) from None
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return list(self._names)

    def __eq__(self, other):
        return tuple(self._values) == (tuple(other) if isinstance(other, (Row, tuple, list)) else other)

    def __hash__(self):
        return hash(tuple(self._values))

    def __repr__(self):
        return f"Row({dict(zip(self._names, self._values))})"


def row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(names, tuple(values))


@functools.lru_cache(maxsize=1024)
def translate(sql):
    """`?` -> `%s` outside quotes, and a literal `%` -> `%%`, so SQL written with `?` placeholders runs on psycopg."""
    out, quote = [], None
    for ch in sql:
        if quote:
            out.append("%%" if ch == "%" else ch)
            if ch == quote:
                quote = None                      # a doubled '' closes and reopens, which comes out the same
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


class Conn:
    def __init__(self, url):
        self.url = url
        self._tx = []                             # open `with conn:` blocks, innermost last
        self._c = self._open()

    def _open(self):
        return psycopg.connect(self.url, autocommit=True, row_factory=row_factory, connect_timeout=10,
                               keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)

    def _retrying(self, call):
        try:
            return call()
        except psycopg.OperationalError as e:
            if self._tx or not self._c.closed and not self._c.broken:
                raise                              # a real SQL problem, or we are mid-transaction: do not guess
            log.warning("database connection lost (%s); reconnecting", e)
            self._c = self._open()
            return call()

    def execute(self, sql, params=()):
        sql = translate(sql)
        return self._retrying(lambda: self._c.execute(sql, tuple(params)))

    def executemany(self, sql, seq):
        sql = translate(sql)
        seq = [tuple(p) for p in seq]

        def run():
            with self._c.cursor() as cur:
                cur.executemany(sql, seq)
                return cur
        return self._retrying(run)

    def __enter__(self):
        cm = self._c.transaction()
        cm.__enter__()
        self._tx.append(cm)
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._tx.pop().__exit__(exc_type, exc, tb)

    def commit(self):
        """Nothing to do: statements commit on their own and `with conn:` commits at its end."""

    @property
    def in_transaction(self):
        return bool(self._tx)

    def close(self):
        self._c.close()
