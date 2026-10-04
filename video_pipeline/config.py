from __future__ import annotations

from dataclasses import asdict, dataclass


PIPELINE_VERSION = "1.0.0"
STATE_SCHEMA_VERSION = 1

# 07 候选已经由用户人工选定。参数只在这里定义，UI 和命令拼接不得另存一份。
LADA_VERSION = "0.11.0"
LADA_RESTORATION_MODEL = "basicvsrpp-v1.2"
LADA_DETECTION_MODEL = "v4-fast"
LADA_DEVICE = "cuda:0"
LADA_FP16 = False
LADA_MAX_CLIP_LENGTH = 180
LADA_INTERMEDIATE_CODEC = "ffv1"
LADA_INTERMEDIATE_LEVEL = 3
LADA_RESTORATION_MODEL_SHA256 = (
    "d404152576ce64fb5b2f315c03062709dac4f5f8548934866cd01c823c8104ee"
)
LADA_DETECTION_MODEL_SHA256 = (
    "9a6b660d1d3e3797d39515e08b0e72fcc59815f38279faa7a4ab374ab2c1e3b4"
)

QUALITY_DENOISE_FILTER = "hqdn3d=0.6:0.45:1.2:0.9"
QUALITY_FINAL_CAS_FILTER = "cas=strength=0.2"

RIFE_RELEASE = "20221029"
RIFE_MODEL = "rife-v4.6"
RIFE_GPU_ID = 0
RIFE_THREADS = "2:2:2"
DEFAULT_RIFE_CHUNK_FRAMES = 900

FINAL_VIDEO_CODEC = "libx264"
FINAL_PRESET = "veryfast"
FINAL_CRF = 12
FINAL_PIXEL_FORMAT = "yuv420p"

CONCEPTUAL_STAGE_ORDER = (
    "demosaic",
    "quality_enhancement",
    "frame_interpolation_2x",
    "final_encode_and_audio_mux",
    "technical_validation",
)


@dataclass(frozen=True)
class PipelineOptions:
    enable_demosaic: bool
    enable_quality_enhancement: bool
    enable_frame_interpolation_2x: bool

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)

    @property
    def has_visual_processing(self) -> bool:
        return any(self.to_dict().values())


def build_execution_plan(options: PipelineOptions) -> list[dict[str, object]]:
    """返回唯一的正式阶段顺序，供执行器、测试和 UI 说明共用。"""
    plan: list[dict[str, object]] = []
    if options.enable_demosaic:
        plan.append(
            {
                "stage": "demosaic",
                "method": "lada",
                "restoration_model": LADA_RESTORATION_MODEL,
                "detection_model": LADA_DETECTION_MODEL,
                "device": LADA_DEVICE,
                "fp16": LADA_FP16,
                "max_clip_length": LADA_MAX_CLIP_LENGTH,
                "intermediate_codec": LADA_INTERMEDIATE_CODEC,
            }
        )
    if options.enable_quality_enhancement:
        plan.append(
            {
                "stage": "quality_enhancement",
                "method": "ffmpeg_lossless_intermediate_then_final_cas",
                "denoise_filter": QUALITY_DENOISE_FILTER,
                "final_filter": QUALITY_FINAL_CAS_FILTER,
                "resolution_scale": None,
            }
        )
    if options.enable_frame_interpolation_2x:
        plan.append(
            {
                "stage": "frame_interpolation_2x",
                "method": "rife_ncnn_vulkan_chunked",
                "model": RIFE_MODEL,
                "gpu_id": RIFE_GPU_ID,
                "threads": RIFE_THREADS,
                "factor": 2,
                "tail_policy": "encode_2n_minus_1_frames",
                "intermediate_codec": LADA_INTERMEDIATE_CODEC,
            }
        )
    if options.has_visual_processing:
        plan.append(
            {
                "stage": "final_encode_and_audio_mux",
                "method": "single_final_encode_then_copy_original_audio",
                "video_codec": FINAL_VIDEO_CODEC,
                "preset": FINAL_PRESET,
                "crf": FINAL_CRF,
                "pixel_format": FINAL_PIXEL_FORMAT,
                "copy_audio_from_original": True,
                "include_subtitles": False,
            }
        )
    else:
        plan.append(
            {
                "stage": "passthrough_remux",
                "method": "copy_first_video_and_all_audio",
                "copy_video": True,
                "copy_audio_from_original": True,
                "include_subtitles": False,
            }
        )
    plan.append(
        {
            "stage": "technical_validation",
            "method": "ffprobe_ffmpeg_pts_framemd5_sha256",
            "visual_acceptance": False,
        }
    )
    return plan
