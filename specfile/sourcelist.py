# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import collections
from typing import TYPE_CHECKING, List, Optional, SupportsIndex, overload

from specfile.macros import Macros
from specfile.sections import Section
from specfile.tags import Comments

if TYPE_CHECKING:
    from specfile.specfile import Specfile


class SourcelistEntry:
    """
    Class that represents a spec file source/patch in a %sourcelist/%patchlist.

    Attributes:
        location: Literal location of the source/patch as stored in the spec file.
        comments: List of comments associated with the source/patch.
    """

    def __init__(
        self, location: str, comments: Comments, context: Optional["Specfile"] = None
    ) -> None:
        """
        Constructs a `SourceListEntry` object.

        Args:
            location: Literal location of the source/patch as stored in the spec file.
            comments: List of comments associated with the source/patch.
            context: `Specfile` instance that defines the context for macro expansions.

        Returns:
            Constructed instance of `SourceListEntry` class.
        """
        self.location = location
        self.comments = comments.copy()
        self._context = context

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SourcelistEntry):
            return NotImplemented
        return self.location == other.location and self.comments == other.comments

    def __repr__(self) -> str:
        comments = repr(self.comments)
        return f"SourcelistEntry('{self.location}', {comments})"

    @property
    def expanded_location(self) -> str:
        """URL of the source/patch after expanding macros."""
        if self._context:
            return self._context.expand(self.location)
        return Macros.expand(self.location)


class Sourcelist(collections.UserList):
    """
    Class that represents entries in a %sourcelist/%patchlist section.

    Attributes:
        data: List of individual sources/patches.
    """

    def __init__(
        self,
        data: Optional[List[SourcelistEntry]] = None,
        remainder: Optional[List[str]] = None,
    ) -> None:
        """
        Constructs a `Sourcelist` object.

        Args:
            data: List of individual sources/patches.
            remainder: Leftover lines in a section that can't be parsed into sources/patches.

        Returns:
            Constructed instance of `Sourcelist` class.
        """
        super().__init__()
        if data is not None:
            self.data = data.copy()
        self._remainder = remainder.copy() if remainder is not None else []

    def __repr__(self) -> str:
        data = repr(self.data)
        remainder = repr(self._remainder)
        return f"Sourcelist({data}, {remainder})"

    @overload
    def __getitem__(self, i: SupportsIndex) -> SourcelistEntry:
        pass

    @overload
    def __getitem__(self, i: slice) -> "Sourcelist":
        pass

    def __getitem__(self, i):
        if isinstance(i, slice):
            return Sourcelist(self.data[i], self._remainder)
        else:
            return self.data[i]

    def copy(self) -> "Sourcelist":
        return Sourcelist(self.data, self._remainder)

    @classmethod
    def parse(
        cls, section: Section, context: Optional["Specfile"] = None
    ) -> "Sourcelist":
        """
        Parses a section into sources/patches.

        Args:
            section: %sourcelist/%patchlist section.
            context: `Specfile` instance that defines the context for macro expansions.

        Returns:
            Constructed instance of `Sourcelist` class.
        """
        data = []
        buffer: List[str] = []
        for line in section:
            if line and not line.lstrip().startswith("#"):
                data.append(SourcelistEntry(line, Comments.parse(buffer), context))
                buffer = []
            else:
                buffer.append(line)
        return cls(data, buffer)

    def get_raw_section_data(self) -> List[str]:
        """
        Reconstructs section data from sources/patches.

        Returns:
            List of lines forming the reconstructed section data.
        """
        result = []
        for source in self.data:
            result.extend(source.comments.get_raw_data())
            result.append(source.location)
        result.extend(self._remainder)
        return result
