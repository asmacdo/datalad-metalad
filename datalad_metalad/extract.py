# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""
Run a dataset-level metadata extractor on a dataset
or run a file-level metadata extractor on a file
"""
import logging
import tempfile
import time
from os import curdir
from pathlib import Path
from typing import Dict, Iterable,  List, Optional, Tuple, Type, Union
from uuid import UUID

from dataclasses import dataclass

from datalad.distribution.dataset import Dataset
from datalad.interface.base import Interface
from datalad.interface.base import build_doc
from datalad.interface.utils import eval_results
from datalad.distribution.dataset import (
    datasetmethod,
    EnsureDataset,
    require_dataset,
)
from datalad.metadata.extractors.base import BaseMetadataExtractor

from .extractors.base import (
    DataOutputCategory,
    DatasetMetadataExtractor,
    FileInfo,
    FileMetadataExtractor,
    MetadataExtractor,
    MetadataExtractorBase
)

from datalad.support.annexrepo import AnnexRepo
from datalad.support.gitrepo import GitRepo
from datalad.support.constraints import (
    EnsureNone,
    EnsureStr
)
from datalad.support.param import Parameter

from dataladmetadatamodel.common import get_top_nodes_and_metadata_root_record
from dataladmetadatamodel.filetree import FileTree
from dataladmetadatamodel.mapper.gitmapper.objectreference import \
    flush_object_references
from dataladmetadatamodel.mapper.gitmapper.utils import lock_backend, \
    unlock_backend
from dataladmetadatamodel.metadata import ExtractorConfiguration, Metadata
from dataladmetadatamodel.metadatapath import MetadataPath
from dataladmetadatamodel.metadatasource import ImmediateMetadataSource, \
    LocalGitMetadataSource, MetadataSource

from .extractors.base import ExtractorResult
from .utils import args_to_dict


__docformat__ = "restructuredtext"

default_mapper_family = "git"

lgr = logging.getLogger("datalad.metadata.extract")


@dataclass
class ExtractionParameter:
    realm: Union[AnnexRepo, GitRepo]
    source_dataset: Dataset
    source_dataset_id: UUID
    extractor_class: Union[type(MetadataExtractor), type(FileMetadataExtractor)]
    extractor_name: str
    extractor_arguments: Dict[str, str]
    dataset_tree_path: MetadataPath
    file_tree_path: MetadataPath
    root_primary_data_version: str
    source_primary_data_version: str
    agent_name: str
    agent_email: str


@build_doc
class Extract(Interface):
    """Run a metadata extractor on a dataset or file.

    This command distinguishes between dataset-level extraction and
    file-level extraction.

    If no "path" argument is given, the command
    assumes that a given extractor is a dataset-level extractor and
    executes it on the dataset that is given by the current working
    directory or by the "-d" argument.

    If a path is given, the command assumes that the given extractor is
    a file-level extractor and executes it on the file that is given as
    path parameter. If the file level extractor requests the content of
    a file that is not present, the command might "get" the file content
    to make it locally available.

    [NOT IMPLEMENTED YET] The extractor configuration can be
    parameterized with key-value pairs given as additional arguments.

    The results are written into the repository of the source dataset
    or into the repository of the dataset given by the "-i" parameter.
    If the same extractor is executed on the same element (dataset or
    file) with the same configuration, any existing results will be
    overwritten.

    Examples:

      Use the metalad_core_file-extractor to extract metadata from the
      file "subdir/data_file_1.txt". The dataset is given by the
      current working directory:

        $ datalad meta-extract metalad_core_file subdir/data_file_1.txt

      Use the metalad_core_file-extractor to extract metadata from the
      file "subdir/data_file_1.txt" in the dataset ds0001.

        $ datalad meta-extract -d ds0001 metalad_core_file subdir/data_file_1.txt

      Use the metalad_core_dataset-extractor to extract dataset-level
      metadata from the dataset given by the current working directory.

        $ datalad meta-extract metalad_core_dataset

      Use the metalad_core_dataset-extractor to extract dataset-level
      metadata from the dataset in /datasets/ds0001.

        $ datalad meta-extract -d /datasets/ds0001 metalad_core_dataset

      The command can also take legacy datalad-metalad extractors and
      will execute them in either "content" or "dataset" mode, depending
      on the presence of the "path"-parameter.
    """
    result_renderer = "tailored"

    _params_ = dict(
        extractorname=Parameter(
            args=("extractorname",),
            metavar="EXTRACTOR_NAME",
            doc="Name of a metadata extractor to be executed."),
        path=Parameter(
            args=("path",),
            metavar="FILE",
            nargs="?",
            doc="""Path of a file or dataset to extract metadata
            from. If this argument is provided, we assume a file
            extractor is requested, if the path is not given, or
            if it identifies the root of a dataset, i.e. "", we
            assume a dataset level metadata extractor is
            specified.""",
            constraints=EnsureStr() | EnsureNone()),
        dataset=Parameter(
            args=("-d", "--dataset"),
            doc="""Dataset to extract metadata from. If no dataset
            is given, the dataset is determined by the current work
            directory.""",
            constraints=EnsureDataset() | EnsureNone()),
        into=Parameter(
            args=("-i", "--into"),
            doc="""Dataset to extract metadata into. This must be
            the dataset from which we extract metadata itself (the
            default) or a parent dataset of the dataset from
            which we extract metadata.""",
            constraints=EnsureDataset() | EnsureNone()),
        extractorargs=Parameter(
            args=("extractorargs",),
            metavar="EXTRACTOR_ARGUMENTS",
            doc="""Extractor arguments""",
            nargs="*",
            constraints=EnsureStr() | EnsureNone()))

    @staticmethod
    @datasetmethod(name="meta_extract")
    @eval_results
    def __call__(
            extractorname: str,
            path: Optional[str] = None,
            dataset: Optional[Union[Dataset, str]] = None,
            into: Optional[Union[Dataset, str]] = None,
            extractorargs: Optional[List[str]] = None):

        # Get basic arguments
        source_dataset = require_dataset(
            dataset or curdir,
            purpose="extract metadata",
            check_installed=path is not None)

        if not source_dataset.repo:
            raise ValueError(f"No dataset found in {dataset or curdir}.")

        source_primary_data_version = source_dataset.repo.get_hexsha()

        if into:
            into_dataset = require_dataset(
                into,
                purpose="extract metadata",
                check_installed=True)
            realm = into_dataset.repo
            root_primary_data_version = into_dataset.repo.get_hexsha()       # TODO: check for adjusted/managed branch, use get_corresponding_branch
        else:
            into_dataset = None
            realm = source_dataset.repo
            root_primary_data_version = source_primary_data_version

        extractor_class = get_extractor_class(extractorname)
        dataset_tree_path, file_tree_path = get_path_info(
            source_dataset,
            Path(path) if path else None,
            into_dataset.pathobj if into_dataset else None)

        extraction_parameters = ExtractionParameter(
            realm,
            source_dataset,
            UUID(source_dataset.id),
            extractor_class,
            extractorname,
            args_to_dict(extractorargs),
            dataset_tree_path,
            file_tree_path,
            root_primary_data_version,
            source_primary_data_version,
            source_dataset.config.get("user.name"),
            source_dataset.config.get("user.email"))

        # If a path is given, we assume file-level metadata extraction is
        # requested, and the extractor class is  a subclass of
        # FileMetadataExtractor. If oath is not given, we assume that
        # dataset-level extraction is requested and the extractor
        # class is a subclass of DatasetMetadataExtractor
        if path and path != "--":
            yield from do_file_extraction(extraction_parameters)
        else:
            yield from do_dataset_extraction(extraction_parameters)

        return


def do_dataset_extraction(ep: ExtractionParameter):

    if not issubclass(ep.extractor_class, MetadataExtractorBase):

        lgr.info(
            "performing legacy dataset level metadata "
            "extraction for dataset at at %s",
            ep.source_dataset.path)

        yield from legacy_extract_dataset(ep)
        return

    lgr.info(
        "extracting dataset level metadata for dataset at %s",
        ep.source_dataset.path)

    assert issubclass(ep.extractor_class, DatasetMetadataExtractor)

    extractor = ep.extractor_class(
        ep.source_dataset,
        ep.source_primary_data_version,
        ep.extractor_arguments)

    yield from perform_dataset_metadata_extraction(ep, extractor)


def do_file_extraction(ep: ExtractionParameter):

    if not issubclass(ep.extractor_class, MetadataExtractorBase):

        lgr.info(
            "performing legacy file level metadata "
            "extraction for file at %s/%s",
            ep.source_dataset.path,
            ep.file_tree_path)

        yield from legacy_extract_file(ep)
        return

    lgr.info(
        "performing file level extracting for file at %s/%s",
        ep.source_dataset.path,
        ep.file_tree_path)

    assert issubclass(ep.extractor_class, FileMetadataExtractor)
    file_info = get_file_info(ep.source_dataset, ep.file_tree_path)
    extractor = ep.extractor_class(
        ep.source_dataset,
        ep.source_primary_data_version,
        file_info,
        ep.extractor_arguments)

    ensure_content_availability(extractor, file_info)

    yield from perform_file_metadata_extraction(ep, extractor)


def perform_file_metadata_extraction(ep: ExtractionParameter,
                                     extractor: FileMetadataExtractor):

    output_category = extractor.get_data_output_category()
    if output_category == DataOutputCategory.IMMEDIATE:

        # Process immediate results
        result = extractor.extract(None)
        if result.extraction_success:
            add_file_metadata_source(
                ep,
                result,
                ImmediateMetadataSource(result.immediate_data))

        result.datalad_result_dict["action"] = "meta_extract"
        yield result.datalad_result_dict

    elif output_category == DataOutputCategory.FILE:

        # Process file-based results
        with tempfile.NamedTemporaryFile(mode="bw+") as temporary_file_info:
            result = extractor.extract(temporary_file_info)
            if result.extraction_success:
                add_file_metadata(
                    ep,
                    result,
                    Path(temporary_file_info.name))
            result.datalad_result_dict["action"] = "meta_extract"
            yield result.datalad_result_dict

    elif output_category == DataOutputCategory.DIRECTORY:

        # Process directory results
        raise NotImplementedError

    lgr.info(
        f"added file metadata result to realm {repr(ep.realm)}, "
        f"dataset tree path {repr(ep.dataset_tree_path)}, "
        f"file tree path {repr(ep.file_tree_path)}")

    return


def perform_dataset_metadata_extraction(ep: ExtractionParameter,
                                        extractor: DatasetMetadataExtractor):

    output_category = extractor.get_data_output_category()
    if output_category == DataOutputCategory.IMMEDIATE:
        # Process inline results
        result = extractor.extract(None)
        if result.extraction_success:
            add_dataset_metadata_source(
                ep,
                result,
                ImmediateMetadataSource(result.immediate_data))

        result.datalad_result_dict["action"] = "meta_extract"
        yield result.datalad_result_dict

    elif output_category == DataOutputCategory.FILE:
        # Process file-based results
        with tempfile.NamedTemporaryFile(mode="bw+") as temporary_file_info:
            result = extractor.extract(temporary_file_info)
            if result.extraction_success:
                add_dataset_metadata(
                    ep,
                    result,
                    Path(temporary_file_info.name))
            result.datalad_result_dict["action"] = "meta_extract"
            yield result.datalad_result_dict

    elif output_category == DataOutputCategory.DIRECTORY:
        # Process directory results
        raise NotImplementedError

    lgr.info(
        f"added dataset metadata result to realm {repr(ep.realm)}, "
        f"dataset tree path {repr(ep.dataset_tree_path)})")

    return


def get_extractor_class(extractor_name: str) -> Union[
                                            Type[DatasetMetadataExtractor],
                                            Type[FileMetadataExtractor]]:

    """ Get an extractor from its name """
    from pkg_resources import iter_entry_points

    entry_points = list(
        iter_entry_points("datalad.metadata.extractors", extractor_name))

    if not entry_points:
        raise ValueError(
            "Requested metadata extractor '{}' not available".format(
                extractor_name))

    entry_point, ignored_entry_points = entry_points[-1], entry_points[:-1]
    lgr.debug(
        "Using metadata extractor %s from distribution %s",
        extractor_name,
        entry_point.dist.project_name)

    # Inform about overridden entry points
    for ignored_entry_point in ignored_entry_points:
        lgr.warning(
            "Metadata extractor %s from distribution %s overrides "
            "metadata extractor from distribution %s",
            extractor_name,
            entry_point.dist.project_name,
            ignored_entry_point.dist.project_name)

    return entry_point.load()


def get_file_info(dataset: Dataset,
                  file_path: MetadataPath) -> FileInfo:
    """
    Get information about the file in the dataset or
    None, if the file is not part of the dataset.
    """

    # Convert the metadata file-path into a system file path
    path = Path(file_path)
    try:
        relative_path = path.relative_to(dataset.pathobj)
    except ValueError:
        relative_path = path

    path = dataset.pathobj / relative_path

    path_status = (list(dataset.status(
        path,
        result_renderer="disabled")) or [None])[0]

    if path_status is None:
        raise FileNotFoundError(
            "file not found: {}".format(path))

    if path_status["state"] == "untracked":
        raise ValueError(
            "file not tracked: {}".format(path))

    # noinspection PyUnresolvedReferences
    return FileInfo(
        type=path_status["type"],
        git_sha_sum=path_status["gitshasum"],
        byte_size=path_status.get("bytesize", 0),
        state=path_status["state"],
        path=path_status["path"],  # TODO: use the dataset-tree path here?
        intra_dataset_path=path_status["path"][len(dataset.path) + 1:])


def get_path_info(dataset: Dataset,
                  element_path: Optional[Path],
                  into_dataset_path: Optional[Path] = None
                  ) -> Tuple[MetadataPath, MetadataPath]:
    """
    Determine the dataset tree path and the file tree path.

    If the path is absolute, we can determine the containing dataset
    and the metadatasets around it. If the path is not an element of
    a locally known dataset, we signal an error.

    If the path is relative, we convert it to an absolute path
    by appending it to the dataset or current directory and perform
    the above check.
    """
    full_dataset_path = Path(dataset.path).resolve()
    if into_dataset_path is None:
        dataset_tree_path = MetadataPath("")
    else:
        full_into_dataset_path = into_dataset_path.resolve()
        dataset_tree_path = MetadataPath(
            full_dataset_path.relative_to(full_into_dataset_path))

    if element_path is None:
        return dataset_tree_path, MetadataPath("")

    if element_path.is_absolute():
        full_file_path = element_path
    else:
        full_file_path = full_dataset_path / element_path

    file_tree_path = full_file_path.relative_to(full_dataset_path)

    return dataset_tree_path, MetadataPath(file_tree_path)


def ensure_content_availability(extractor: FileMetadataExtractor,
                                file_info: FileInfo):

    if extractor.is_content_required():
        for result in extractor.dataset.get(path={file_info.path},
                                            get_data=True,
                                            return_type="generator",
                                            result_renderer="disabled"):
            if result.get("status", "") == "error":
                lgr.error(
                    "cannot make content of {} available in dataset {}".format(
                        file_info.path, extractor.dataset))
                return
        lgr.debug(
            "requested content {}:{} available".format(
                extractor.dataset.path, file_info.intra_dataset_path))


def add_file_metadata_source(ep: ExtractionParameter,
                             result: ExtractorResult,
                             metadata_source: MetadataSource):

    realm = str(ep.realm.pathobj)

    lock_backend(realm)

    tree_version_list, uuid_set, mrr = get_top_nodes_and_metadata_root_record(
        default_mapper_family,
        realm,
        ep.source_dataset_id,
        ep.source_primary_data_version,
        ep.dataset_tree_path,
        auto_create=True)

    file_tree = mrr.get_file_tree()
    if file_tree is None:
        file_tree = FileTree(default_mapper_family, realm)
        mrr.set_file_tree(file_tree)

    if ep.file_tree_path in file_tree:
        file_level_metadata = file_tree.get_metadata(ep.file_tree_path)
    else:
        file_level_metadata = Metadata(default_mapper_family, realm)
        file_tree.add_metadata(ep.file_tree_path, file_level_metadata)

    add_metadata_source(file_level_metadata, ep, result, metadata_source)

    tree_version_list.save()
    uuid_set.save()
    flush_object_references(realm)

    unlock_backend(realm)


def add_dataset_metadata_source(ep: ExtractionParameter,
                                result: ExtractorResult,
                                metadata_source: MetadataSource):

    realm = str(ep.realm.pathobj)

    lock_backend(realm)

    tree_version_list, uuid_set, mrr = get_top_nodes_and_metadata_root_record(
        default_mapper_family,
        realm,
        ep.source_dataset_id,
        ep.source_primary_data_version,
        ep.dataset_tree_path,
        auto_create=True)

    dataset_level_metadata = mrr.get_dataset_level_metadata()
    if dataset_level_metadata is None:
        dataset_level_metadata = Metadata(default_mapper_family, realm)
        mrr.set_dataset_level_metadata(dataset_level_metadata)

    add_metadata_source(dataset_level_metadata, ep, result, metadata_source)

    tree_version_list.save()
    uuid_set.save()
    flush_object_references(realm)

    unlock_backend(realm)


def add_metadata_source(metadata: Metadata,
                        ep: ExtractionParameter,
                        result: ExtractorResult,
                        metadata_source: MetadataSource):

    metadata.add_extractor_run(
        time.time(),
        ep.extractor_name,
        ep.agent_name,
        ep.agent_email,
        ExtractorConfiguration(
            result.extractor_version,
            result.extraction_parameter),
        metadata_source)


def add_file_metadata(ep: ExtractionParameter,
                      result: ExtractorResult,
                      metadata_content_file: Path):

    # copy the temporary file content into the git repo
    git_object_hash = copy_file_to_git(metadata_content_file, ep.realm)

    add_file_metadata_source(ep, result, LocalGitMetadataSource(
        ep.realm.pathobj,
        git_object_hash))


def add_dataset_metadata(ep: ExtractionParameter,
                         result: ExtractorResult,
                         metadata_content_file: Path):

    # copy the temporary file content into the git repo
    git_object_hash = copy_file_to_git(metadata_content_file, ep.realm)

    add_dataset_metadata_source(ep, result, LocalGitMetadataSource(
        ep.realm.pathobj,
        git_object_hash))


def copy_file_to_git(file_path: Path, realm: Union[AnnexRepo, GitRepo]):
    arguments = [
        f"--git-dir={realm.pathobj / '.git'}",
        "hash-object", "-w", "--", str(file_path)]
    return realm.call_git_oneline(arguments)


def ensure_legacy_path_availability(ep: ExtractionParameter, path: str):
    for result in ep.source_dataset.get(path=path,
                                        get_data=True,
                                        return_type="generator",
                                        result_renderer="disabled"):

        if result.get("status", "") == "error":
            lgr.error(
                "cannot make content of {} available "
                "in dataset {}".format(
                    path, ep.source_dataset))
            return

    lgr.debug(
        "requested content {}:{} available".format(
            ep.source_dataset.path, path))


def ensure_legacy_content_availability(ep: ExtractionParameter,
                                       extractor: MetadataExtractor,
                                       operation: str,
                                       status: List[dict]):

    try:
        for required_element in extractor.get_required_content(
                    ep.source_dataset,
                    operation,
                    status):

            ensure_legacy_path_availability(ep, required_element.path)
    except AttributeError:
        pass


def legacy_extract_dataset(ep: ExtractionParameter) -> Iterable[dict]:

    if issubclass(ep.extractor_class, MetadataExtractor):

        # Metalad legacy extractor
        status = [{
            "type": "dataset",
            "path": str(ep.realm.pathobj / Path(ep.dataset_tree_path)),
            "state": "clean",
            "gitshasum": ep.source_primary_data_version
        }]
        extractor = ep.extractor_class()
        ensure_legacy_content_availability(ep, extractor, "dataset", status)

        for result in extractor(ep.source_dataset,
                                ep.source_primary_data_version,
                                "dataset",
                                status):

            if result["status"] == "ok":
                extractor_result = ExtractorResult(
                    "0.1",
                    extractor.get_state(ep.source_dataset),
                    True,
                    result,
                    result["metadata"])

                add_dataset_metadata_source(
                    ep,
                    extractor_result,
                    ImmediateMetadataSource(extractor_result.immediate_data))

            yield result

    elif issubclass(ep.extractor_class, BaseMetadataExtractor):

        # Datalad legacy extractor
        path = str(ep.realm.pathobj / Path(ep.dataset_tree_path))
        if ep.extractor_class.NEEDS_CONTENT:
            ensure_legacy_path_availability(ep, path)

        extractor = ep.extractor_class(ep.source_dataset, [path])
        dataset_result, _ = extractor.get_metadata(True, False)

        extractor_result = ExtractorResult("0.1", {}, True, {}, dataset_result)
        add_dataset_metadata_source(
            ep,
            extractor_result,
            ImmediateMetadataSource(extractor_result.immediate_data))

    else:
        raise ValueError(
            f"unknown extractor class: {type(ep.extractor_class).__name__}")


def legacy_extract_file(ep: ExtractionParameter) -> Iterable[dict]:

    if issubclass(ep.extractor_class, MetadataExtractor):

        # Metalad legacy extractor
        status = [{
            "type": "file",
            "path": str(
                ep.realm.pathobj
                / Path(ep.dataset_tree_path)
                / Path(ep.file_tree_path)),
            "state": "clean",
            "gitshasum": ep.source_primary_data_version
        }]
        extractor = ep.extractor_class()
        ensure_legacy_content_availability(ep, extractor, "content", status)

        for result in extractor(ep.source_dataset,
                                ep.source_primary_data_version,
                                "content",
                                status):

            if result["status"] == "ok":
                extractor_result = ExtractorResult(
                    "0.1",
                    extractor.get_state(ep.source_dataset),
                    True,
                    result,
                    result["metadata"])

                add_file_metadata_source(
                    ep,
                    extractor_result,
                    ImmediateMetadataSource(extractor_result.immediate_data))

            yield result

    elif issubclass(ep.extractor_class, BaseMetadataExtractor):

        # Datalad legacy extractor
        path = str(ep.realm.pathobj / Path(ep.dataset_tree_path) / Path(ep.file_tree_path))
        if ep.extractor_class.NEEDS_CONTENT:
            ensure_legacy_path_availability(ep, path)

        extractor = ep.extractor_class(ep.source_dataset, [path])
        _, file_result = extractor.get_metadata(False, True)

        for path, metadata in file_result:
            extractor_result = ExtractorResult("0.1", {}, True, {}, metadata)
            add_file_metadata_source(
                ep,
                extractor_result,
                ImmediateMetadataSource(extractor_result.immediate_data))

    else:
        raise ValueError(
            f"unknown extractor class: {type(ep.extractor_class).__name__}")
