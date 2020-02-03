# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
# pylint: skip-file
import enum
from dataclasses import dataclass
from typing import Callable, Dict, List, NewType, NoReturn, Optional, Tuple, Union

import ankirspy  # pytype: disable=import-error

import anki.backend_pb2 as pb
import anki.buildinfo
from anki import hooks
from anki.models import AllTemplateReqs
from anki.sound import AVTag, SoundOrVideoTag, TTSTag
from anki.types import assert_impossible_literal

assert ankirspy.buildhash() == anki.buildinfo.buildhash

SchedTimingToday = pb.SchedTimingTodayOut


class Interrupted(Exception):
    pass


class StringError(Exception):
    def __str__(self) -> str:
        return self.args[0]  # pylint: disable=unsubscriptable-object


class NetworkError(StringError):
    pass


class IOError(StringError):
    pass


class DBError(StringError):
    pass


class TemplateError(StringError):
    def q_side(self) -> bool:
        return self.args[1]


class AnkiWebError(StringError):
    pass


class AnkiWebAuthFailed(Exception):
    pass


def proto_exception_to_native(err: pb.BackendError) -> Exception:
    val = err.WhichOneof("value")
    if val == "interrupted":
        return Interrupted()
    elif val == "network_error":
        return NetworkError(err.network_error.info)
    elif val == "io_error":
        return IOError(err.io_error.info)
    elif val == "db_error":
        return DBError(err.db_error.info)
    elif val == "template_parse":
        return TemplateError(err.template_parse.info, err.template_parse.q_side)
    elif val == "invalid_input":
        return StringError(err.invalid_input.info)
    elif val == "ankiweb_auth_failed":
        return AnkiWebAuthFailed()
    elif val == "ankiweb_misc_error":
        return AnkiWebError(err.ankiweb_misc_error.info)
    else:
        assert_impossible_literal(val)


def proto_template_reqs_to_legacy(
    reqs: List[pb.TemplateRequirement],
) -> AllTemplateReqs:
    legacy_reqs = []
    for (idx, req) in enumerate(reqs):
        kind = req.WhichOneof("value")
        # fixme: sorting is for the unit tests - should check if any
        # code depends on the order
        if kind == "any":
            legacy_reqs.append((idx, "any", sorted(req.any.ords)))
        elif kind == "all":
            legacy_reqs.append((idx, "all", sorted(req.all.ords)))
        else:
            l: List[int] = []
            legacy_reqs.append((idx, "none", l))
    return legacy_reqs


def av_tag_to_native(tag: pb.AVTag) -> AVTag:
    val = tag.WhichOneof("value")
    if val == "sound_or_video":
        return SoundOrVideoTag(filename=tag.sound_or_video)
    else:
        return TTSTag(
            field_text=tag.tts.field_text,
            lang=tag.tts.lang,
            voices=list(tag.tts.voices),
            other_args=list(tag.tts.other_args),
            speed=tag.tts.speed,
        )


@dataclass
class TemplateReplacement:
    field_name: str
    current_text: str
    filters: List[str]


TemplateReplacementList = List[Union[str, TemplateReplacement]]


@dataclass
class MediaSyncDownloadedChanges:
    changes: int


@dataclass
class MediaSyncDownloadedFiles:
    files: int


@dataclass
class MediaSyncUploaded:
    files: int
    deletions: int


@dataclass
class MediaSyncRemovedFiles:
    files: int


MediaSyncProgress = Union[
    MediaSyncDownloadedChanges,
    MediaSyncDownloadedFiles,
    MediaSyncUploaded,
    MediaSyncRemovedFiles,
]


class ProgressKind(enum.Enum):
    MediaSyncProgress = 0


@dataclass
class Progress:
    kind: ProgressKind
    val: Union[MediaSyncProgress]


def proto_replacement_list_to_native(
    nodes: List[pb.RenderedTemplateNode],
) -> TemplateReplacementList:
    results: TemplateReplacementList = []
    for node in nodes:
        if node.WhichOneof("value") == "text":
            results.append(node.text)
        else:
            results.append(
                TemplateReplacement(
                    field_name=node.replacement.field_name,
                    current_text=node.replacement.current_text,
                    filters=list(node.replacement.filters),
                )
            )
    return results


def proto_progress_to_native(progress: pb.Progress) -> Progress:
    kind = progress.WhichOneof("value")
    if kind == "media_sync":
        ikind = progress.media_sync.WhichOneof("value")
        pkind = ProgressKind.MediaSyncProgress
        if ikind == "downloaded_changes":
            return Progress(
                kind=pkind,
                val=MediaSyncDownloadedChanges(progress.media_sync.downloaded_changes),
            )
        elif ikind == "downloaded_files":
            return Progress(
                kind=pkind,
                val=MediaSyncDownloadedFiles(progress.media_sync.downloaded_files),
            )
        elif ikind == "uploaded":
            up = progress.media_sync.uploaded
            return Progress(
                kind=pkind,
                val=MediaSyncUploaded(files=up.files, deletions=up.deletions),
            )
        elif ikind == "removed_files":
            return Progress(
                kind=pkind, val=MediaSyncRemovedFiles(progress.media_sync.removed_files)
            )
        else:
            assert_impossible_literal(ikind)
    assert_impossible_literal(kind)


class RustBackend:
    def __init__(self, col_path: str, media_folder_path: str, media_db_path: str):
        init_msg = pb.BackendInit(
            collection_path=col_path,
            media_folder_path=media_folder_path,
            media_db_path=media_db_path,
        )
        self._backend = ankirspy.open_backend(init_msg.SerializeToString())
        self._backend.set_progress_callback(self._on_progress)

    def _on_progress(self, progress_bytes: bytes) -> bool:
        progress = pb.Progress()
        progress.ParseFromString(progress_bytes)
        native_progress = proto_progress_to_native(progress)
        return hooks.rust_progress_callback(True, native_progress)

    def _run_command(
        self, input: pb.BackendInput, release_gil: bool = False
    ) -> pb.BackendOutput:
        input_bytes = input.SerializeToString()
        output_bytes = self._backend.command(input_bytes, release_gil)
        output = pb.BackendOutput()
        output.ParseFromString(output_bytes)
        kind = output.WhichOneof("value")
        if kind == "error":
            raise proto_exception_to_native(output.error)
        else:
            return output

    def template_requirements(
        self, template_fronts: List[str], field_map: Dict[str, int]
    ) -> AllTemplateReqs:
        input = pb.BackendInput(
            template_requirements=pb.TemplateRequirementsIn(
                template_front=template_fronts, field_names_to_ordinals=field_map
            )
        )
        output = self._run_command(input).template_requirements
        reqs: List[pb.TemplateRequirement] = output.requirements  # type: ignore
        return proto_template_reqs_to_legacy(reqs)

    def sched_timing_today(
        self,
        created_secs: int,
        created_mins_west: int,
        now_secs: int,
        now_mins_west: int,
        rollover: int,
    ) -> SchedTimingToday:
        return self._run_command(
            pb.BackendInput(
                sched_timing_today=pb.SchedTimingTodayIn(
                    created_secs=created_secs,
                    created_mins_west=created_mins_west,
                    now_secs=now_secs,
                    now_mins_west=now_mins_west,
                    rollover_hour=rollover,
                )
            )
        ).sched_timing_today

    def render_card(
        self, qfmt: str, afmt: str, fields: Dict[str, str], card_ord: int
    ) -> Tuple[TemplateReplacementList, TemplateReplacementList]:
        out = self._run_command(
            pb.BackendInput(
                render_card=pb.RenderCardIn(
                    question_template=qfmt,
                    answer_template=afmt,
                    fields=fields,
                    card_ordinal=card_ord,
                )
            )
        ).render_card

        qnodes = proto_replacement_list_to_native(out.question_nodes)  # type: ignore
        anodes = proto_replacement_list_to_native(out.answer_nodes)  # type: ignore

        return (qnodes, anodes)

    def local_minutes_west(self, stamp: int) -> int:
        return self._run_command(
            pb.BackendInput(local_minutes_west=stamp)
        ).local_minutes_west

    def strip_av_tags(self, text: str) -> str:
        return self._run_command(pb.BackendInput(strip_av_tags=text)).strip_av_tags

    def extract_av_tags(
        self, text: str, question_side: bool
    ) -> Tuple[str, List[AVTag]]:
        out = self._run_command(
            pb.BackendInput(
                extract_av_tags=pb.ExtractAVTagsIn(
                    text=text, question_side=question_side
                )
            )
        ).extract_av_tags
        native_tags = list(map(av_tag_to_native, out.av_tags))

        return out.text, native_tags

    def expand_clozes_to_reveal_latex(self, text: str) -> str:
        return self._run_command(
            pb.BackendInput(expand_clozes_to_reveal_latex=text)
        ).expand_clozes_to_reveal_latex

    def add_file_to_media_folder(self, desired_name: str, data: bytes) -> str:
        return self._run_command(
            pb.BackendInput(
                add_file_to_media_folder=pb.AddFileToMediaFolderIn(
                    desired_name=desired_name, data=data
                )
            )
        ).add_file_to_media_folder

    def sync_media(
        self, hkey: str, media_folder: str, media_db: str, endpoint: str
    ) -> None:
        self._run_command(
            pb.BackendInput(
                sync_media=pb.SyncMediaIn(
                    hkey=hkey,
                    media_folder=media_folder,
                    media_db=media_db,
                    endpoint=endpoint,
                )
            ),
            release_gil=True,
        )
