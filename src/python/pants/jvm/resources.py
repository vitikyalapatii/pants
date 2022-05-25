# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import itertools
import logging
from itertools import chain

from pants.core.target_types import ResourcesFieldSet, ResourcesGeneratorFieldSet
from pants.core.util_rules import stripped_source_files
from pants.core.util_rules.source_files import SourceFilesRequest
from pants.core.util_rules.stripped_source_files import StrippedSourceFiles
from pants.core.util_rules.system_binaries import ZipBinary
from pants.engine.fs import Digest, MergeDigests
from pants.engine.internals.selectors import MultiGet
from pants.engine.process import Process, ProcessResult
from pants.engine.rules import Get, collect_rules, rule
from pants.engine.target import SourcesField
from pants.engine.unions import UnionRule
from pants.jvm import compile
from pants.jvm.compile import (
    ClasspathDependenciesRequest,
    ClasspathEntry,
    ClasspathEntryRequest,
    ClasspathEntryRequests,
    CompileResult,
    FallibleClasspathEntries,
    FallibleClasspathEntry,
)
from pants.util.logging import LogLevel

logger = logging.getLogger(__name__)


class JvmResourcesRequest(ClasspathEntryRequest):
    field_sets = (
        ResourcesFieldSet,
        ResourcesGeneratorFieldSet,
    )


@rule(desc="Assemble resources")
async def assemble_resources_jar(
    zip: ZipBinary,
    request: JvmResourcesRequest,
) -> FallibleClasspathEntry:
    # Request the component's direct dependency classpath, and additionally any prerequisite.
    # Filter out any dependencies that are generated by our current target so that each resource
    # only appears in a single input JAR.
    # NOTE: Generated dependencies will have the same dependencies as the current target, so we
    # don't need to inspect those dependencies.
    optional_prereq_request = [*((request.prerequisite,) if request.prerequisite else ())]
    fallibles = await MultiGet(
        Get(FallibleClasspathEntries, ClasspathEntryRequests(optional_prereq_request)),
        Get(FallibleClasspathEntries, ClasspathDependenciesRequest(request, ignore_generated=True)),
    )
    direct_dependency_classpath_entries = FallibleClasspathEntries(
        itertools.chain(*fallibles)
    ).if_all_succeeded()

    if direct_dependency_classpath_entries is None:
        return FallibleClasspathEntry(
            description=str(request.component),
            result=CompileResult.DEPENDENCY_FAILED,
            output=None,
            exit_code=1,
        )

    source_files = await Get(
        StrippedSourceFiles,
        SourceFilesRequest([tgt.get(SourcesField) for tgt in request.component.members]),
    )

    output_filename = f"{request.component.representative.address.path_safe_spec}.resources.jar"
    output_files = [output_filename]

    resources_jar_input_digest = source_files.snapshot.digest
    resources_jar_result = await Get(
        ProcessResult,
        Process(
            argv=[
                zip.path,
                output_filename,
                *source_files.snapshot.files,
            ],
            description="Build resources JAR for {request.component}",
            input_digest=resources_jar_input_digest,
            output_files=output_files,
            level=LogLevel.DEBUG,
        ),
    )

    cpe = ClasspathEntry(resources_jar_result.output_digest, output_files, [])

    merged_cpe_digest = await Get(
        Digest,
        MergeDigests(chain((cpe.digest,), (i.digest for i in direct_dependency_classpath_entries))),
    )

    merged_cpe = ClasspathEntry.merge(
        digest=merged_cpe_digest, entries=[cpe, *direct_dependency_classpath_entries]
    )

    return FallibleClasspathEntry(output_filename, CompileResult.SUCCEEDED, merged_cpe, 0)


def rules():
    return [
        *collect_rules(),
        *compile.rules(),
        *stripped_source_files.rules(),
        UnionRule(ClasspathEntryRequest, JvmResourcesRequest),
    ]
