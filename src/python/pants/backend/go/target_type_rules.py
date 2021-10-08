# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
import os.path
from collections import defaultdict
from dataclasses import dataclass

from pants.backend.go.target_types import (
    GoBinaryDependenciesField,
    GoBinaryMainPackage,
    GoBinaryMainPackageField,
    GoBinaryMainPackageRequest,
    GoFirstPartyPackageDependenciesField,
    GoFirstPartyPackageSourcesField,
    GoFirstPartyPackageSubpathField,
    GoFirstPartyPackageTarget,
    GoImportPathField,
    GoModPackageSourcesField,
    GoModTarget,
    GoThirdPartyModulePathField,
    GoThirdPartyModuleVersionField,
    GoThirdPartyPackageDependenciesField,
    GoThirdPartyPackageTarget,
)
from pants.backend.go.util_rules import first_party_pkg, import_analysis
from pants.backend.go.util_rules.first_party_pkg import FirstPartyPkgInfo, FirstPartyPkgInfoRequest
from pants.backend.go.util_rules.go_mod import (
    GoModInfo,
    GoModInfoRequest,
    OwningGoMod,
    OwningGoModRequest,
)
from pants.backend.go.util_rules.import_analysis import GoStdLibImports
from pants.backend.go.util_rules.third_party_pkg import (
    ThirdPartyModuleInfo,
    ThirdPartyModuleInfoRequest,
    ThirdPartyPkgInfo,
    ThirdPartyPkgInfoRequest,
)
from pants.base.exceptions import ResolveError
from pants.base.specs import AddressSpecs, AscendantAddresses, DescendantAddresses
from pants.core.goals.tailor import group_by_dir
from pants.engine.addresses import Address, AddressInput
from pants.engine.fs import PathGlobs, Paths
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import (
    GeneratedTargets,
    GenerateTargetsRequest,
    InferDependenciesRequest,
    InferredDependencies,
    InjectDependenciesRequest,
    InjectedDependencies,
    InvalidFieldException,
    Targets,
    WrappedTarget,
)
from pants.engine.unions import UnionRule
from pants.option.global_options import FilesNotFoundBehavior
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel

logger = logging.getLogger(__name__)


class InjectGoFirstPartyPackageDependenciesRequest(InjectDependenciesRequest):
    inject_for = GoFirstPartyPackageDependenciesField


@rule
async def inject_go_package_dependencies(
    request: InjectGoFirstPartyPackageDependenciesRequest,
) -> InjectedDependencies:
    owning_go_mod = await Get(OwningGoMod, OwningGoModRequest(request.dependencies_field.address))
    return InjectedDependencies([owning_go_mod.address])


# TODO: Figure out how to merge (or not) this with ResolvedImportPaths as a base class.
@dataclass(frozen=True)
class ImportPathToPackages:
    mapping: FrozenDict[str, tuple[Address, ...]]


@rule
async def map_import_paths_to_packages() -> ImportPathToPackages:
    mapping: dict[str, list[Address]] = defaultdict(list)
    all_targets = await Get(Targets, AddressSpecs([DescendantAddresses("")]))
    for tgt in all_targets:
        if not tgt.has_field(GoImportPathField):
            continue
        import_path = tgt[GoImportPathField].value
        mapping[import_path].append(tgt.address)
    frozen_mapping = FrozenDict({ip: tuple(tgts) for ip, tgts in mapping.items()})
    return ImportPathToPackages(frozen_mapping)


# TODO: Use dependency injection. This doesn't actually look at the Sources field.
class InferGoPackageDependenciesRequest(InferDependenciesRequest):
    infer_from = GoFirstPartyPackageSourcesField


@rule
async def infer_go_dependencies(
    request: InferGoPackageDependenciesRequest,
    std_lib_imports: GoStdLibImports,
    package_mapping: ImportPathToPackages,
) -> InferredDependencies:
    addr = request.sources_field.address
    pkg_info = await Get(FirstPartyPkgInfo, FirstPartyPkgInfoRequest(addr))

    inferred_dependencies = []
    for import_path in (*pkg_info.imports, *pkg_info.test_imports, *pkg_info.xtest_imports):
        if import_path in std_lib_imports:
            continue
        candidate_packages = package_mapping.mapping.get(import_path, ())
        if len(candidate_packages) > 1:
            # TODO: Use ExplicitlyProvidedDependencies.maybe_warn_of_ambiguous_dependency_inference standard
            # way of doing disambiguation.
            logger.warning(
                f"Ambiguous mapping for import path {import_path} on packages at addresses: {candidate_packages}"
            )
        elif len(candidate_packages) == 1:
            inferred_dependencies.append(candidate_packages[0])
        else:
            logger.debug(
                f"Unable to infer dependency for import path '{import_path}' "
                f"in go_first_party_package at address '{addr}'."
            )

    return InferredDependencies(inferred_dependencies)


class InjectGoThirdPartyPackageDependenciesRequest(InjectDependenciesRequest):
    inject_for = GoThirdPartyPackageDependenciesField


@rule
async def inject_go_third_party_package_dependencies(
    request: InjectGoThirdPartyPackageDependenciesRequest,
    std_lib_imports: GoStdLibImports,
    package_mapping: ImportPathToPackages,
) -> InjectedDependencies:
    addr = request.dependencies_field.address
    wrapped_target = await Get(WrappedTarget, Address, addr)
    tgt = wrapped_target.target

    owning_go_mod = await Get(OwningGoMod, OwningGoModRequest(addr))
    go_mod_info = await Get(GoModInfo, GoModInfoRequest(owning_go_mod.address))
    pkg_info = await Get(
        ThirdPartyPkgInfo,
        ThirdPartyPkgInfoRequest(
            module_path=tgt[GoThirdPartyModulePathField].value,
            version=tgt[GoThirdPartyModuleVersionField].value,
            import_path=tgt[GoImportPathField].value,
            go_mod_stripped_digest=go_mod_info.stripped_digest,
        ),
    )

    inferred_dependencies = []
    for import_path in pkg_info.imports:
        if import_path in std_lib_imports:
            continue

        candidate_packages = package_mapping.mapping.get(import_path, ())
        if len(candidate_packages) > 1:
            # TODO: Use ExplicitlyProvidedDependencies.maybe_warn_of_ambiguous_dependency_inference standard
            # way of doing disambiguation.
            logger.warning(
                f"Ambiguous mapping for import path {import_path} on packages at addresses: {candidate_packages}"
            )
        elif len(candidate_packages) == 1:
            inferred_dependencies.append(candidate_packages[0])
        else:
            logger.debug(
                f"Unable to infer dependency for import path '{import_path}' "
                f"in go_third_party_package at address '{addr}'."
            )

    return InjectedDependencies(inferred_dependencies)


# -----------------------------------------------------------------------------------------------
# Generate `go_first_party_package` and `go_third_party_package` targets
# -----------------------------------------------------------------------------------------------


class GenerateTargetsFromGoModRequest(GenerateTargetsRequest):
    generate_from = GoModTarget


@rule(
    desc=(
        "Generate `go_first_party_package` and `go_third_party_package` targets from `go_mod` "
        "target"
    ),
    level=LogLevel.DEBUG,
)
async def generate_targets_from_go_mod(
    request: GenerateTargetsFromGoModRequest, files_not_found_behavior: FilesNotFoundBehavior
) -> GeneratedTargets:
    generator_addr = request.generator.address
    go_mod_info, go_paths = await MultiGet(
        Get(GoModInfo, GoModInfoRequest(generator_addr)),
        Get(
            Paths,
            PathGlobs,
            request.generator[GoModPackageSourcesField].path_globs(files_not_found_behavior),
        ),
    )
    all_module_info = await MultiGet(
        Get(
            ThirdPartyModuleInfo,
            ThirdPartyModuleInfoRequest(
                module_path=module_descriptor.path,
                version=module_descriptor.version,
                go_mod_stripped_digest=go_mod_info.stripped_digest,
            ),
        )
        for module_descriptor in go_mod_info.modules
    )

    dir_to_filenames = group_by_dir(go_paths.files)
    matched_dirs = [dir for dir, filenames in dir_to_filenames.items() if filenames]

    def create_first_party_package_tgt(dir: str) -> GoFirstPartyPackageTarget:
        go_mod_spec_path = generator_addr.spec_path
        assert dir.startswith(
            go_mod_spec_path
        ), f"the dir {dir} should start with {go_mod_spec_path}"

        if not go_mod_spec_path:
            subpath = dir
        elif dir == go_mod_spec_path:
            subpath = ""
        else:
            subpath = dir[len(go_mod_spec_path) + 1 :]

        import_path = f"{go_mod_info.import_path}/{subpath}" if subpath else go_mod_info.import_path

        return GoFirstPartyPackageTarget(
            {
                GoImportPathField.alias: import_path,
                GoFirstPartyPackageSubpathField.alias: subpath,
                GoFirstPartyPackageSourcesField.alias: tuple(
                    sorted(os.path.join(subpath, f) for f in dir_to_filenames[dir])
                ),
            },
            # E.g. `src/go:mod#./subdir`.
            generator_addr.create_generated(f"./{subpath}"),
        )

    first_party_pkgs = (create_first_party_package_tgt(dir) for dir in matched_dirs)

    def create_third_party_package_tgt(pkg_info: ThirdPartyPkgInfo) -> GoThirdPartyPackageTarget:
        return GoThirdPartyPackageTarget(
            {
                GoThirdPartyModulePathField.alias: pkg_info.module_path,
                GoThirdPartyModuleVersionField.alias: pkg_info.version,
                GoImportPathField.alias: pkg_info.import_path,
            },
            # E.g. `src/go:mod#github.com/google/uuid`.
            generator_addr.create_generated(pkg_info.import_path),
        )

    third_party_pkgs = (
        create_third_party_package_tgt(pkg_info)
        for module_info in all_module_info
        for pkg_info in module_info.values()
    )
    return GeneratedTargets(request.generator, (*first_party_pkgs, *third_party_pkgs))


# -----------------------------------------------------------------------------------------------
# The `main` field for `go_binary`
# -----------------------------------------------------------------------------------------------


@rule
async def determine_main_pkg_for_go_binary(
    request: GoBinaryMainPackageRequest,
) -> GoBinaryMainPackage:
    addr = request.field.address
    if request.field.value:
        wrapped_specified_tgt = await Get(
            WrappedTarget,
            AddressInput,
            AddressInput.parse(request.field.value, relative_to=addr.spec_path),
        )
        if not wrapped_specified_tgt.target.has_field(GoFirstPartyPackageSourcesField):
            raise InvalidFieldException(
                f"The {repr(GoBinaryMainPackageField.alias)} field in target {addr} must point to "
                "a `go_first_party_package` target, but was the address for a "
                f"`{wrapped_specified_tgt.target.alias}` target.\n\n"
                "Hint: you should normally not specify this field so that Pants will find the "
                "`go_first_party_package` target for you. (Pants generates "
                "`go_first_party_package` targets based on the `go_mod` target)."
            )
        return GoBinaryMainPackage(wrapped_specified_tgt.target.address)

    candidate_targets = await Get(Targets, AddressSpecs([AscendantAddresses(addr.spec_path)]))
    relevant_pkg_targets = [
        tgt
        for tgt in candidate_targets
        if (
            tgt.has_field(GoFirstPartyPackageSubpathField)
            and tgt[GoFirstPartyPackageSubpathField].full_dir_path == addr.spec_path
        )
    ]
    if len(relevant_pkg_targets) == 1:
        return GoBinaryMainPackage(relevant_pkg_targets[0].address)

    wrapped_tgt = await Get(WrappedTarget, Address, addr)
    alias = wrapped_tgt.target.alias
    if not relevant_pkg_targets:
        raise ResolveError(
            f"The `{alias}` target {addr} requires that there is a `go_first_party_package` "
            f"target for its directory {addr.spec_path}, but none were found.\n\n"
            "Have you added a `go_mod` target (which will generate `go_first_party_package` "
            "targets)?"
        )
    raise ResolveError(
        f"There are multiple `go_first_party_package` targets for the same directory of the "
        "`{alias}` target {addr}: {addr.spec_path}. It is ambiguous what to use as the `main` "
        "package.\n\n"
        f"To fix, please either set the `main` field for `{addr} or remove these "
        "`go_first_party_package` targets so that only one remains: "
        f"{sorted(tgt.address.spec for tgt in relevant_pkg_targets)}"
    )


class InjectGoBinaryMainDependencyRequest(InjectDependenciesRequest):
    inject_for = GoBinaryDependenciesField


@rule
async def inject_go_binary_main_dependency(
    request: InjectGoBinaryMainDependencyRequest,
) -> InjectedDependencies:
    wrapped_tgt = await Get(WrappedTarget, Address, request.dependencies_field.address)
    main_pkg = await Get(
        GoBinaryMainPackage,
        GoBinaryMainPackageRequest(wrapped_tgt.target[GoBinaryMainPackageField]),
    )
    return InjectedDependencies([main_pkg.address])


def rules():
    return (
        *collect_rules(),
        *first_party_pkg.rules(),
        *import_analysis.rules(),
        UnionRule(InjectDependenciesRequest, InjectGoFirstPartyPackageDependenciesRequest),
        UnionRule(InferDependenciesRequest, InferGoPackageDependenciesRequest),
        UnionRule(InjectDependenciesRequest, InjectGoThirdPartyPackageDependenciesRequest),
        UnionRule(InjectDependenciesRequest, InjectGoBinaryMainDependencyRequest),
        UnionRule(GenerateTargetsRequest, GenerateTargetsFromGoModRequest),
    )
