"""Lexical path helpers and CCC alias canonicalization.

Everything here is purely lexical: policy classification runs on BranchFS
status output, which may name paths that no longer exist on disk, and must
never be influenced by symlinks the (untrusted) agent created.
"""

import posixpath


def normalize(path):
    """Normalize an absolute POSIX path lexically.

    Collapses ``//``, ``.`` and ``..`` segments and strips trailing slashes.
    Raises ``ValueError`` for empty or relative paths.
    """
    if not path or not path.startswith("/"):
        raise ValueError("absolute path required, got %r" % (path,))
    norm = posixpath.normpath(path)
    return norm


def is_within(path, prefix):
    """Return True if ``path`` is ``prefix`` or inside it (boundary-safe).

    ``/storage/user2`` is *not* within ``/storage/user``.
    """
    path = normalize(path)
    prefix = normalize(prefix)
    if prefix == "/":
        return True
    return path == prefix or path.startswith(prefix + "/")


class AliasMap:
    """Translates agent-visible alias paths to canonical underlay paths.

    CCC exposes the same user storage at ``/home/$USER`` and ``/storage/user``
    (the home may be the storage root itself or a subdirectory of it).  Policy
    must evaluate one canonical namespace, so scopes and changes are both
    passed through ``canonicalize`` before comparison.
    """

    def __init__(self, aliases):
        # aliases: {alias_prefix: canonical_prefix}; longest alias wins.
        self._aliases = sorted(
            ((normalize(a), normalize(c)) for a, c in dict(aliases).items()),
            key=lambda item: len(item[0]),
            reverse=True,
        )

    @classmethod
    def for_home(cls, user, home_subdir="", storage_user="/storage/user",
                 home_root="/home"):
        """Build the standard CCC home alias.

        ``home_subdir=""`` means ``/home/$USER`` is the storage-user root
        itself; otherwise it is ``<storage_user>/<home_subdir>``.
        """
        home = posixpath.join(home_root, user)
        canonical = storage_user if not home_subdir else posixpath.join(
            storage_user, home_subdir)
        return cls({home: canonical})

    def canonicalize(self, path):
        path = normalize(path)
        for alias, canonical in self._aliases:
            if path == alias:
                return canonical
            if path.startswith(alias + "/"):
                return canonical + path[len(alias):]
        return path

    def to_dict(self):
        return {alias: canonical for alias, canonical in self._aliases}
