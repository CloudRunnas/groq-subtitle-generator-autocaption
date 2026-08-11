import os
import tempfile
import subprocess
import logging
from typing import Optional, List, Dict
from pathlib import Path
import asyncio
from contextlib import asynccontextmanager

from services.transcription_service import TranscriptionService
from services.translation_service import TranslationService
from services.subtitle_service import SubtitleService
from services.alignment_service import AlignmentService
from services.karaoke_service import KaraokeService
from models.requests import TranscriptionResult
from utils.config import get_settings

logger = logging.getLogger(__name__)

class VideoProcessingService:
    def __init__(self):
        self.settings = get_settings()
        self.transcription_service = TranscriptionService()
        self.translation_service = TranslationService()
        self.subtitle_service = SubtitleService()
        self.alignment_service = AlignmentService()
        self.karaoke_service = KaraokeService()
        self.alignment_service._device = self.settings.whisperx_device

    @asynccontextmanager
    async def temporary_file(self, suffix: str = ""):
        """context manager for temporary files that are automatically cleaned up"""
        temp_file = None
        try:
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            temp_file.close()
            yield temp_file.name
        finally:
            if temp_file and os.path.exists(temp_file.name):
                try:
                    os.unlink(temp_file.name)
                    logger.debug(f"Cleaned up temporary file: {temp_file.name}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup temporary file {temp_file.name}: {str(e)}")

    #processes video with streaming approach so no permanent files are saved
    async def process_video_streaming(self, video_data: bytes, target_language: str, 
                                    source_language: Optional[str] = None) -> bytes:
        """
        process video
        """
        try:
            logger.info(f"Starting streaming video processing")
            
            # save video temporarily for processing
            async with self.temporary_file(suffix=".mp4") as temp_video_path:
                with open(temp_video_path, 'wb') as f:
                    f.write(video_data)
                
                # extract audio temporarily
                async with self.temporary_file(suffix=".wav") as temp_audio_path:
                    await self._extract_audio(temp_video_path, temp_audio_path)
                    
                    # transcribe audio
                    transcription_result = await self.transcription_service.transcribe_audio(temp_audio_path)
                    
                    # detect language if not provided
                    if not source_language:
                        source_language = transcription_result.detected_language or "en"
                    
                    # translate if needed
                    if source_language != target_language:
                        translated_segments = await self.translation_service.translate_segments(
                            transcription_result.segments, source_language, target_language
                        )
                        
                        final_transcription = TranscriptionResult(
                            text=" ".join([segment.text for segment in translated_segments]),
                            segments=translated_segments,
                            detected_language=source_language,
                            confidence=transcription_result.confidence
                        )
                    else:
                        final_transcription = transcription_result
                    
                    srt_content = await self.subtitle_service.generate_srt_content(final_transcription)
                    
                    async with self.temporary_file(suffix=".srt") as temp_subtitle_path:
                        with open(temp_subtitle_path, 'w', encoding='utf-8') as f:
                            f.write(srt_content)
                        
                        return await self._render_video_to_bytes(temp_video_path, temp_subtitle_path)
                        
        except Exception as e:
            logger.error(f"Error in streaming video processing: {str(e)}")
            raise

    async def _extract_audio(self, video_path: str, audio_path: str):
        """extract audio from video"""
        try:
            cmd = [
                'ffmpeg', '-i', video_path,
                '-vn', '-acodec', 'pcm_s16le',
                '-ar', '16000', '-ac', '1',
                '-y', audio_path
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                raise Exception(f"Audio extraction failed: {stderr.decode()}")
                
            logger.info("Audio extraction completed")
            
        except Exception as e:
            logger.error(f"Error extracting audio: {str(e)}")
            raise

    async def _render_video_to_bytes(
        self,
        video_path: str,
        subtitle_path: str,
        fonts_dir: Optional[str] = None,
    ) -> bytes:
        """render video with subtitles (SRT or ASS) and return as bytes"""
        try:
            async with self.temporary_file(suffix=".mp4") as temp_output_path:
                # Escape path for ffmpeg subtitles filter (esp. Windows / special chars)
                escaped_sub = (
                    subtitle_path
                    .replace("\\", "/")
                    .replace(":", "\\:")
                    .replace("'", "\\'")
                )
                vf = f"subtitles='{escaped_sub}'"
                if fonts_dir:
                    escaped_fonts = (
                        fonts_dir.replace("\\", "/")
                        .replace(":", "\\:")
                        .replace("'", "\\'")
                    )
                    vf = f"subtitles='{escaped_sub}':fontsdir='{escaped_fonts}'"
                cmd = [
                    'ffmpeg', '-i', video_path,
                    '-vf', vf,
                    '-c:a', 'copy',
                    '-c:v', 'libx264',
                    '-y', temp_output_path
                ]
                
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                stdout, stderr = await process.communicate()
                
                if process.returncode != 0:
                    raise Exception(f"video rendering failed: {stderr.decode()}")
                
                # read the rendered video into meory
                with open(temp_output_path, 'rb') as f:
                    video_bytes = f.read()
                
                logger.info("video rendering completed")
                return video_bytes
                
        except Exception as e:
            logger.error(f"error rendering video: {str(e)}")
            raise

    async def render_with_karaoke(
        self,
        video_data: bytes,
        word_timings: list,
        style_template=None,
        fonts_dir: Optional[str] = None,
    ) -> bytes:
        """Render video with karaoke ASS burned in."""
        video_size = await self.get_video_dimensions(video_data)
        ass_content = self.karaoke_service.generate_ass_content(
            word_timings,
            video_size=video_size,
            style_template=style_template,
        )
        async with self.temporary_file(suffix=".mp4") as temp_video_path:
            with open(temp_video_path, 'wb') as f:
                f.write(video_data)
            async with self.temporary_file(suffix=".ass") as temp_ass_path:
                with open(temp_ass_path, 'w', encoding='utf-8') as f:
                    f.write(ass_content)
                return await self._render_video_to_bytes(
                    temp_video_path,
                    temp_ass_path,
                    fonts_dir=fonts_dir,
                )

    async def get_video_dimensions(self, video_data: bytes):
        """Return (width, height) of the primary video stream."""
        try:
            info = await self.get_video_info(video_data)
            for stream in info.get("streams", []):
                if stream.get("codec_type") == "video":
                    width = int(stream.get("width") or 0)
                    height = int(stream.get("height") or 0)
                    if width > 0 and height > 0:
                        return width, height
        except Exception as e:
            logger.warning(f"Could not probe video dimensions, using 1920x1080: {e}")
        return 1920, 1080

    async def align_and_build_karaoke(
        self,
        video_data: bytes,
        cue_segments: list,
        language_code: str = "de",
    ) -> list:
        """
        Extract audio, run WhisperX alignment, return word timings.
        cue_segments: list of {text, start, end}
        """
        async with self.temporary_file(suffix=".mp4") as temp_video_path:
            with open(temp_video_path, 'wb') as f:
                f.write(video_data)
            async with self.temporary_file(suffix=".wav") as temp_audio_path:
                await self._extract_audio(temp_video_path, temp_audio_path)
                return await self.alignment_service.align_segments(
                    temp_audio_path,
                    cue_segments,
                    language_code=language_code,
                    release_after=True,
                )

    async def get_video_info(self, video_data: bytes) -> Dict:
        """Get video information from bytes"""
        try:
            async with self.temporary_file(suffix=".mp4") as temp_video_path:
                # write video data to temporary file
                with open(temp_video_path, 'wb') as f:
                    f.write(video_data)
                
                cmd = [
                    'ffprobe', '-v', 'quiet', '-print_format', 'json',
                    '-show_format', '-show_streams', temp_video_path
                ]
                
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                stdout, stderr = await process.communicate()
                
                if process.returncode != 0:
                    raise Exception(f"Failed to get video info: {stderr.decode()}")
                
                import json
                return json.loads(stdout.decode())
                
        except Exception as e:
            logger.error(f"Error getting video info: {str(e)}")
            raise

    async def detect_language_from_video(self, video_data: bytes) -> str:
        """detect language from video bytes"""
        try:
            async with self.temporary_file(suffix=".mp4") as temp_video_path:
                # write video data to temporary file
                with open(temp_video_path, 'wb') as f:
                    f.write(video_data)
                
                # extract audio temporarily
                async with self.temporary_file(suffix=".wav") as temp_audio_path:
                    await self._extract_audio(temp_video_path, temp_audio_path)
                    return await self.transcription_service.detect_language(temp_audio_path)
                    
        except Exception as e:
            logger.error(f"Error detecting language: {str(e)}")
            raise 