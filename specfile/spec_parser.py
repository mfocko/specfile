# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Iterator, List, Optional, Set, Tuple

import rpm

from specfile.exceptions import RPMException
from specfile.macros import Macros
from specfile.sections import Section
from specfile.tags import Tags
from specfile.utils import capture_stderr, get_filename_from_location
from specfile.value_parser import ConditionalMacroExpansion, ShellExpansion, ValueParser

logger = logging.getLogger(__name__)


class SpecParser:
    """
    Class that represents a spec file parser.

    Attributes:
        sourcedir: Path to sources and patches.
        macros: List of extra macro definitions.
        force_parse: Whether to attempt to parse the spec file even if one or more
          sources required to be present at parsing time are not available.
          Such sources include sources referenced from shell expansions
          in tag values and sources included using the %include directive.
        spec: `rpm.spec` instance representing parsed spec file.
        tainted: Indication that parsing of the spec file was forced and one or more
          sources required to be present at parsing time were not available
          and were replaced with dummy files.
    """

    def __init__(
        self,
        sourcedir: Path,
        macros: Optional[List[Tuple[str, str]]] = None,
        force_parse: bool = False,
    ) -> None:
        self.sourcedir = sourcedir
        self.macros = macros.copy() if macros is not None else []
        self.force_parse = force_parse
        self.spec = None
        self.tainted = False

    def __repr__(self) -> str:
        sourcedir = repr(self.sourcedir)
        macros = repr(self.macros)
        force_parse = repr(self.force_parse)
        return f"SpecParser({sourcedir}, {macros}, {force_parse})"

    @contextlib.contextmanager
    def _make_dummy_sources(
        self, sources: Set[str], non_empty_sources: Set[str]
    ) -> Iterator[List[Path]]:
        """
        Context manager for creating temporary dummy sources to enable a spec file
        to be fully parsed by RPM.

        Args:
            sources: Set of sources to be faked with effectively empty files if they don't exist.
              File signatures will be generated according to extensions.
            non_empty_sources: Set of sources to be faked with text files containing
              dummy content if they don't exist.

        Yields:
            List of paths to each created dummy source.
        """
        # based on rpmFileIsCompressed() in rpmio/rpmfileutil.c in RPM source
        SIGNATURES = [
            (".bz2", b"BZh"),
            (".zip", b"PK00"),
            (".xz", b"\xfd\x37\x7a\x58\x5a\x00"),
            (".zst", b"\x28\xb5\x2f"),
            (".lz", b"LZIP"),
            (".lrz", b"LRZI"),
            (".gz", b"\x1f\x8b"),
            (".7z", b"7z\xbc\xaf\x27\x1c"),
        ]
        # number of bytes that RPM reads to determine the file type
        MAGIC_LENGTH = 13
        dummy_sources = []
        for source in sources:
            filename = get_filename_from_location(source)
            if not filename:
                continue
            path = self.sourcedir / filename
            if path.is_file():
                continue
            dummy_sources.append(path)
            for ext, magic in SIGNATURES:
                if filename.endswith(ext):
                    path.write_bytes(magic.ljust(MAGIC_LENGTH, b"\x00"))
                    break
            else:
                path.write_bytes(MAGIC_LENGTH * b"\x00")
        for source in non_empty_sources:
            filename = get_filename_from_location(source)
            if not filename:
                continue
            path = self.sourcedir / filename
            if path.is_file():
                continue
            dummy_sources.append(path)
            path.write_text("DUMMY")
        yield dummy_sources
        for path in dummy_sources:
            path.unlink()

    @contextlib.contextmanager
    def _sanitize_environment(self) -> Iterator[os._Environ]:
        """
        Context manager for sanitizing the environment for shell expansions.

        Temporarily sets LANG and LC_ALL to C.UTF-8 locale.

        Yields:
            Sanitized environment.
        """
        env = os.environ.copy()

        def restore(key):
            try:
                os.environ[key] = env[key]
            except KeyError:
                del os.environ[key]

        keys = ["LANG", "LC_ALL"]
        for key in keys:
            os.environ[key] = "C.UTF-8"
        try:
            yield os.environ
        finally:
            for key in keys:
                restore(key)

    def _do_parse(
        self, content: str, extra_macros: Optional[List[Tuple[str, str]]] = None
    ) -> Tuple[rpm.spec, bool]:
        """
        Parses the content of a spec file.

        Args:
            content: String representing the content of a spec file.
            extra_macros: List of extra macro definitions.

        Returns:
            Tuple of (parsed spec file as `rpm.spec` instance, indication whether
              at least one file to be included was ignored).

        Raises:
            RPMException, if parsing error occurs.
        """

        def get_rpm_spec(content, flags):
            Macros.reinit()
            for name, value in self.macros + (extra_macros or []):
                Macros.define(name, value)
            Macros.define("_sourcedir", str(self.sourcedir))
            with tempfile.NamedTemporaryFile() as tmp:
                tmp.write(content.encode())
                tmp.flush()
                try:
                    with self._sanitize_environment():
                        with capture_stderr() as stderr:
                            return rpm.spec(tmp.name, flags)
                except ValueError as e:
                    raise RPMException(stderr=stderr) from e

        def collect_sources_referenced_from_tags(content):
            # collect sources referenced from shell expansions in tag values

            def flatten(nodes):
                result = []
                for node in nodes:
                    if isinstance(node, ConditionalMacroExpansion):
                        result.extend(flatten(node.body))
                    else:
                        result.append(node)
                return result

            # source references: %SOURCEN, %{SOURCEN}, %{S:N}
            source_ref_regex = re.compile(r"%((?P<b>{)?[?!]*SOURCE\d+(?(b)})|{S:\d+})")
            sources = set()
            for tag in Tags.parse(Section("package", content.splitlines())):
                # we can expand macros here because the first non-build parse,
                # even though it failed, populated the macro context
                if Macros.expand(tag.value):
                    # tag value doesn't expand to an empty string, so it won't
                    # break parsing, we can skip this tag
                    continue
                refs = []
                for node in flatten(ValueParser.parse(tag.value)):
                    if isinstance(node, ShellExpansion):
                        for m in source_ref_regex.finditer(node.body):
                            refs.append(m.group(0))
                for ref in refs:
                    # we can expand macros here because the first non-build parse,
                    # even though it failed, populated the macro context
                    source = Path(Macros.expand(ref))
                    # ignore files outside of sourcedir
                    if source.parent.samefile(self.sourcedir):
                        sources.add(source.name)
            return sources

        def collect_included_sources(content):
            # collect sources included using %include
            include_regex = re.compile(r"^\s*%include\s+(.*)$")
            lines = content.splitlines()
            sources = set()
            while lines:
                line = lines.pop(0)
                m = include_regex.match(line)
                if not m:
                    continue
                arg = m.group(1)
                while line.endswith("\\"):
                    line = lines.pop(0)
                    arg = arg[:-1] + line
                # we can expand macros here because the first non-build parse,
                # even though it failed, populated the macro context
                source = Path(Macros.expand(arg))
                # ignore files outside of sourcedir
                if source.parent.samefile(self.sourcedir):
                    sources.add(source.name)
            return sources

        tainted = False
        try:
            # do a non-build parse first, to get a list of sources
            spec = get_rpm_spec(content, rpm.RPMSPEC_ANYARCH | rpm.RPMSPEC_FORCE)
            sources = {s for s, _, _ in spec.sources}
            non_empty_sources = set()
        except RPMException:
            if not self.force_parse:
                raise
            else:
                sources = collect_included_sources(content)
                non_empty_sources = collect_sources_referenced_from_tags(content)
                if not sources and not non_empty_sources:
                    # no point in trying again
                    raise
                with self._make_dummy_sources(
                    sources, non_empty_sources
                ) as dummy_sources:
                    if dummy_sources:
                        filelist = "\n".join(str(ds) for ds in dummy_sources)
                        logger.warning(
                            f"Created dummy sources for nonexistent files:\n{filelist}"
                        )
                        tainted = True
                        # do a non-build parse again with dummy sources
                        spec = get_rpm_spec(
                            content, rpm.RPMSPEC_ANYARCH | rpm.RPMSPEC_FORCE
                        )
                        # spec.sources contains also previously collected
                        # non empty sources (if any), remove them
                        sources = {s for s, _, _ in spec.sources} - non_empty_sources

        # workaround RPM lua tables feature/bug
        #
        # RPM lua tables are a global storage that is used by lua macros and in turn
        # also by standard %sources and %patches macros
        #
        # they are initialized when a rpm.spec instance is created and destroyed
        # when a rpm.spec instance is deleted
        #
        # when a variable that holds a rpm.spec instance is assigned a new rpm.spec
        # instance, the old instance is garbage collected afterwards and that destroys
        # lua tables that were (re)initialized when the new instance was created
        #
        # explicitly deleting the old instance before creating a new one prevents this
        del spec

        with self._make_dummy_sources(sources, non_empty_sources):
            # do a full parse with dummy sources
            return get_rpm_spec(content, rpm.RPMSPEC_ANYARCH), tainted

    def parse(
        self, content: str, extra_macros: Optional[List[Tuple[str, str]]] = None
    ) -> None:
        """
        Parses the content of a spec file and updates the `spec` and `tainted` attributes.

        Args:
            content: String representing the content of a spec file.
            extra_macros: List of extra macro definitions.

        Raises:
            RPMException, if parsing error occurs.
        """
        if self.spec:
            # workaround RPM lua tables feature/bug, see above for details
            del self.spec
        self.spec, self.tainted = self._do_parse(content, extra_macros)
