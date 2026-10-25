"""Fallback selection, format matrix and remux/transcode planning."""

from __future__ import annotations

import pytest

from ytharvest.errors import FormatSelectionError, MediaError
from ytharvest.formats.matrix import (
    FormatMatrix,
    audio_family,
    codec_family,
    codec_label,
    is_hdr,
    is_video_only,
    video_height,
)
from ytharvest.formats.planner import choose_container, plan_audio, plan_for, plan_video
from ytharvest.formats.resolver import StreamManifestResolver, build_selector, combined_selector


def vf(format_id, vcodec, height, fps=30, tbr=4000, ext="webm", dynamic_range="SDR", **extra):
    return {
        "format_id": format_id,
        "vcodec": vcodec,
        "acodec": "none",
        "height": height,
        "fps": fps,
        "tbr": tbr,
        "ext": ext,
        "dynamic_range": dynamic_range,
        **extra,
    }


def af(format_id, acodec, tbr=160, ext="webm", **extra):
    return {
        "format_id": format_id,
        "vcodec": "none",
        "acodec": acodec,
        "tbr": tbr,
        "ext": ext,
        **extra,
    }


def pf(format_id, vcodec, acodec, height, ext="mp4", **extra):
    return {
        "format_id": format_id,
        "vcodec": vcodec,
        "acodec": acodec,
        "height": height,
        "ext": ext,
        "tbr": 2500,
        "fps": 30,
        "dynamic_range": "SDR",
        **extra,
    }


# --------------------------------------------------------------------- codecs
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("av01.0.08M.08.0.110.0.0.0.0", "av01"),
        ("vp09.00.10.08", "vp09"),
        ("avc1.640028", "avc1"),
        ("hev1.1.6.L120.90", "hev1"),
        ("none", ""),
        (None, ""),
        ("garbage", "unknown"),
    ],
)
def test_codec_family_parsing(raw, expected):
    assert codec_family(raw) == expected


def test_codec_labels_and_audio_family():
    assert codec_label("av01.0") == "AV1"
    assert codec_label("vp09.0") == "VP9"
    assert codec_label("avc1.640028") == "H.264"
    assert audio_family("mp4a.40.2") == "aac"
    assert audio_family("opus") == "opus"
    assert audio_family("none") == ""


def test_is_video_only_and_height_helpers():
    assert is_video_only(vf("v1", "av01.1", 2160))
    assert video_height({"format_note": "1080p"}) == 1080
    assert is_hdr({"dynamic_range": "HDR"})
    assert not is_hdr({"dynamic_range": "SDR"})


# ------------------------------------------------------------ matrix ranking
def test_matrix_ranks_codecs_by_priority():
    matrix = FormatMatrix(codec_priority=("av01", "vp09", "avc1"))
    av1 = vf("a", "av01.0", 1080)
    vp9 = vf("b", "vp09.0", 1080)
    h264 = vf("c", "avc1.64", 1080)
    ordered = matrix.sorted_videos([h264, av1, vp9], target_height=1080)
    assert [f["format_id"] for f in ordered] == ["a", "b", "c"]


def test_matrix_respects_resolution_cap_and_prefers_exact_target():
    matrix = FormatMatrix(codec_priority=("av01",), max_height=1080)
    over = vf("over", "av01.0", 2160)
    exact = vf("exact", "av01.0", 1080)
    low = vf("low", "av01.0", 720)
    assert not matrix.allowed(over)
    ordered = matrix.sorted_videos([low, over, exact], target_height=1080)
    assert ordered[0]["format_id"] == "exact"
    assert ordered[-1]["format_id"] == "low"


def test_matrix_fps_and_hdr_preferences():
    sdr30 = vf("sdr30", "av01.0", 1080, fps=30)
    sdr60 = vf("sdr60", "av01.0", 1080, fps=60)
    hdr60 = vf("hdr60", "av01.0", 1080, fps=60, dynamic_range="HDR")

    prefer_fps = FormatMatrix(codec_priority=("av01",), prefer_fps=60)
    ranked = prefer_fps.sorted_videos([sdr30, sdr60], target_height=1080)
    assert ranked[0]["format_id"] == "sdr60"

    prefer_hdr = FormatMatrix(codec_priority=("av01",), prefer_fps=60, prefer_hdr=True)
    ranked = prefer_hdr.sorted_videos([sdr60, hdr60], target_height=1080)
    assert ranked[0]["format_id"] == "hdr60"

    sdr_pref = FormatMatrix(codec_priority=("av01",), prefer_fps=60, prefer_hdr=False)
    ranked = sdr_pref.sorted_videos([hdr60, sdr60], target_height=1080)
    assert ranked[0]["format_id"] == "sdr60"


def test_matrix_audio_ranking_prefers_source_quality_for_lossless():
    matrix = FormatMatrix(audio_codec="flac")
    opus160 = af("a1", "opus", tbr=160)
    aac128 = af("a2", "mp4a.40.2", tbr=128)
    ordered = matrix.sorted_audio([aac128, opus160])
    assert ordered[0]["format_id"] == "a1"


# ---------------------------------------------------------------- resolver
def sample_manifest():
    return {
        "formats": [
            vf("v-av1-2160", "av01.0.08M.08", 2160, tbr=12000),
            vf("v-av1-1080", "av01.0.08M.08", 1080, tbr=6000),
            vf("v-vp9-2160", "vp09.00.40.08", 2160, tbr=11000),
            vf("v-vp9-1080", "vp09.00.40.08", 1080, tbr=5000),
            vf("v-h264-1080", "avc1.640028", 1080, tbr=4500),
            vf("v-h264-720", "avc1.4d401f", 720, tbr=2500),
            af("a-opus", "opus", tbr=160),
            af("a-aac", "mp4a.40.2", tbr=128),
            pf("p-best", "avc1.640028", "mp4a.40.2", 360),
        ]
    }


def test_resolver_builds_four_rung_fallback_tree():
    resolver = StreamManifestResolver(FormatMatrix(codec_priority=("av01", "vp09", "avc1")))
    resolved = resolver.resolve(sample_manifest())

    assert resolved.target_height == 2160
    kinds = [c.kind for c in resolved.chain]
    assert kinds[0] == "target"
    # rung 1: AV1 @ 2160 (best codec at target res)
    assert resolved.chain[0].selector.startswith("v-av1-2160+")
    # rung 2: next best codec at target res -> VP9 @ 2160
    second = next(c for c in resolved.chain if c.rung == 2)
    assert second.selector.startswith("v-vp9-2160+")
    # rung 3: lower res at best codec -> AV1 @ 1080 must exist before h264
    lower = [c for c in resolved.chain if c.rung == 3]
    assert lower, "expected lower-resolution rungs"
    assert lower[0].selector.startswith("v-av1-1080+")
    # rung 4: legacy combined stream
    legacy = [c for c in resolved.chain if c.rung == 4]
    assert any(c.selector == "p-best" for c in legacy)
    assert legacy[-1].selector == "bv*+ba/b"


def test_resolver_target_resolution_is_capped():
    matrix = FormatMatrix(codec_priority=("av01", "vp09", "avc1"), max_height=1080)
    resolved = StreamManifestResolver(matrix).resolve(sample_manifest())
    assert resolved.target_height == 1080
    assert resolved.chain[0].selector.startswith("v-av1-1080+")


def test_resolver_target_codec_falls_back_when_target_codec_missing():
    # target 4K AV1 is unavailable: only VP9 @ 2160 and H.264 below
    manifest = {
        "formats": [
            vf("v-vp9-2160", "vp09.00.40.08", 2160),
            vf("v-h264-2160", "avc1.640031", 2160),
            vf("v-h264-720", "avc1.4d401f", 720),
            af("a-opus", "opus", tbr=160),
        ]
    }
    resolved = StreamManifestResolver(
        FormatMatrix(codec_priority=("av01", "vp09", "avc1"))
    ).resolve(manifest)
    assert resolved.target_height == 2160
    assert resolved.chain[0].selector.startswith("v-vp9-2160+")   # best available codec
    rung2 = [c for c in resolved.chain if c.rung == 2]
    assert rung2 and rung2[0].selector.startswith("v-h264-2160+")  # next codec @ target res
    rung3 = [c for c in resolved.chain if c.rung == 3]
    assert rung3 and rung3[0].selector.startswith("v-h264-720+")   # lower res


def test_resolver_picks_matching_audio_container():
    manifest = {
        "formats": [
            vf("v-av1-1080", "av01.0", 1080, ext="mp4"),
            af("a-opus", "opus", tbr=160, ext="webm"),
            af("a-aac", "mp4a.40.2", tbr=128, ext="m4a"),
        ]
    }
    resolved = StreamManifestResolver().resolve(manifest)
    assert resolved.chain[0].selector == "v-av1-1080+a-aac"  # mp4 video + m4a audio


def test_resolver_rejects_empty_manifest():
    with pytest.raises(FormatSelectionError):
        StreamManifestResolver().resolve({"formats": []})


def test_resolver_rejects_missing_formats():
    from ytharvest.errors import ManifestError

    with pytest.raises(ManifestError):
        StreamManifestResolver().resolve({"title": "nothing"})


def test_selector_helpers():
    assert build_selector(vf("v", "av01", 1080), af("a", "opus")) == "v+a"
    assert build_selector(None, af("a", "opus")) == "a"
    assert build_selector(vf("v", "av01", 1080), None) == "v"
    resolved = StreamManifestResolver().resolve(sample_manifest())
    combined = combined_selector(resolved.chain)
    assert "/" in combined
    assert len(set(combined.split("/"))) == len(combined.split("/"))  # deduped


# ------------------------------------------------------------------ planner
def test_plan_copies_matching_opus():
    plan = plan_audio(af("a", "opus"), target="opus")
    assert plan.mode == "copy"
    assert "-c:a" in plan.ffmpeg_args and "copy" in plan.ffmpeg_args
    assert plan.output_ext == "opus"


def test_plan_copies_matching_aac_to_m4a():
    plan = plan_audio(af("a", "mp4a.40.2"), target="aac")
    assert plan.mode == "copy"
    assert plan.output_ext == "m4a"


def test_plan_flac_is_lossless_transcode_with_max_compression():
    plan = plan_audio(af("a", "opus", tbr=160), target="flac")
    assert plan.mode == "transcode"
    assert plan.ffmpeg_args[:4] == ["-c:a", "flac", "-compression_level", "12"]
    assert plan.output_ext == "flac"
    assert "sample" not in " ".join(plan.ffmpeg_args)  # source sample rate preserved


def test_plan_wav_decodes_to_pcm():
    plan = plan_audio(af("a", "opus"), target="wav")
    assert plan.ffmpeg_args == ["-c:a", "pcm_s16le", "-map_metadata", "0"]
    assert plan.output_ext == "wav"


def test_plan_mp3_vbr_vs_cbr():
    vbr = plan_audio(af("a", "opus"), target="mp3", bitrate_mode="vbr")
    assert vbr.ffmpeg_args[:2] == ["-c:a", "libmp3lame"]
    assert "-q:a" in vbr.ffmpeg_args
    cbr = plan_audio(af("a", "opus"), target="mp3", bitrate_mode="cbr", bitrate="320k")
    assert cbr.ffmpeg_args == ["-c:a", "libmp3lame", "-b:a", "320k"]


def test_plan_opus_transcode_marks_vbr_mode():
    plan = plan_audio(af("a", "mp4a.40.2"), target="opus", bitrate_mode="vbr")
    assert plan.mode == "transcode"
    assert "-vbr" in plan.ffmpeg_args
    cbr = plan_audio(af("a", "mp4a.40.2"), target="opus", bitrate_mode="cbr")
    assert cbr.ffmpeg_args[cbr.ffmpeg_args.index("-vbr") + 1] == "off"


def test_plan_unknown_audio_target_rejected():
    with pytest.raises(MediaError):
        plan_audio(af("a", "opus"), target="ogg")


def test_plan_video_chooses_container_by_codec():
    assert choose_container(vf("v", "av01.0", 2160)) == "mp4"
    assert choose_container(vf("v", "vp09.00", 2160)) == "mkv"
    assert choose_container(vf("v", "avc1.64", 1080)) == "mp4"

    plan = plan_video(vf("v", "vp09.00", 1080), af("a", "opus"))
    assert plan.mode == "remux"
    assert plan.output_ext == "mkv"
    assert plan.ffmpeg_args[:4] == ["-c:v", "copy", "-c:a", "copy"]

    mp4 = plan_video(vf("v", "avc1.64", 1080), af("a", "mp4a.40.2"))
    assert mp4.output_ext == "mp4"
    assert "+faststart" in mp4.ffmpeg_args


def test_plan_for_dispatch():
    rules = type("R", (), {"audio_codec": "flac", "bitrate_mode": "vbr", "audio_bitrate": "192k",
                           "target_container": None})()
    assert plan_for("audio", None, af("a", "opus"), rules).output_ext == "flac"
    assert plan_for("video", vf("v", "av01", 1080), af("a", "opus"), rules).video is True
    with pytest.raises(MediaError):
        plan_for("metadata", None, None, rules)
