import asyncio
import os
import shutil
import threading
import traceback
from pathlib import Path
from tkinter import filedialog

import speech_recognition as sr
import utils.audio_utils as au
import utils.config_manager as cm
import whisperx
from models.transcription import Transcription
from moviepy.video.io.VideoFileClip import VideoFileClip
from pydub import AudioSegment
from pydub.silence import split_on_silence
from pytube import YouTube
from pytube.exceptions import RegexMatchError
from utils import constants as c
from utils.enums import AudioSource, TranscriptionMethod
from utils.i18n import _
from utils.path_helper import ROOT_PATH


class MainController:
    def __init__(self, transcription: Transcription, view):
        self.view = view
        self.transcription = transcription
        self._is_mic_recording = False
        self._whisperx_result = None

    # PUBLIC METHODS

    def select_file(self):
        """
        Prompts a file explorer to determine the audio/video file path to transcribe.
        """
        file_path = filedialog.askopenfilename(
            initialdir="/",
            title=_("Select a file"),
            filetypes=[
                (
                    _("All supported files"),
                    c.AUDIO_FILE_EXTENSIONS + c.VIDEO_FILE_EXTENSIONS,
                ),
                (_("Audio files"), c.AUDIO_FILE_EXTENSIONS),
                (_("Video files"), c.VIDEO_FILE_EXTENSIONS),
            ],
        )

        if file_path:
            self.view.on_select_path_success(file_path)

    def select_directory(self):
        """
        Prompts a file explorer to determine the folder path to transcribe.
        """
        dir_path = filedialog.askdirectory()

        if dir_path:
            self.view.on_select_path_success(dir_path)

    def prepare_for_transcription(self, transcription: Transcription):
        """
        Prepares the transcription process based on provided parameters.

        :raises: IndexError if the selected language code is not valid.
        """
        self.transcription = transcription

        try:
            self.view.on_processing_transcription()

            if transcription.source == AudioSource.FILE:
                self._prepare_for_file_transcription(transcription.source_path)
            elif transcription.source == AudioSource.DIRECTORY:
                self._prepare_for_directory_files_transcription(
                    transcription.source_path
                )
            elif transcription.source == AudioSource.MIC:
                self._prepare_for_mic_transcription()
            elif transcription.source == AudioSource.YOUTUBE:
                self._prepare_for_yt_transcription()

        except Exception as e:
            self._handle_exception(e)

    async def _handle_transcription_process(self):
        try:
            path = self.transcription.source_path

            if self.transcription.source == AudioSource.DIRECTORY:
                if files := self._get_transcribable_files_from_dir(path):
                    # Create a list of coroutines for each file transcription task
                    tasks = [self._transcribe_file(file) for file in files]

                    # Run all tasks concurrently
                    await asyncio.gather(*tasks)

                    self.view.display_text(
                        f"Files from '{path}' successfully " f"transcribed."
                    )
                else:
                    raise ValueError(
                        "Error: The directory path is invalid or doesn't contain valid "
                        "file types to transcribe. Please choose another one."
                    )
            else:
                await self._transcribe_file(path)

        except Exception as e:
            self._handle_exception(e)

        finally:
            is_transcription_empty = not self.transcription.text
            self.view.on_processed_transcription(success=is_transcription_empty)

    async def _transcribe_file(self, file_path: Path):
        if self.transcription.method == TranscriptionMethod.WHISPERX.value:
            await self._transcribe_using_whisperx(file_path)
        elif self.transcription.method == TranscriptionMethod.GOOGLE_API.value:
            await self._transcribe_using_google_api(file_path)

        if self.transcription.source in [AudioSource.MIC, AudioSource.YOUTUBE]:
            self.transcription.source_path.unlink()  # Remove tmp file

        if self.transcription.should_autosave:
            self.save_transcription(
                file_path,
                should_autosave=True,
                should_overwrite=self.transcription.should_overwrite,
            )

    def stop_recording_from_mic(self):
        self._is_mic_recording = False

    def save_transcription(
        self, file_path: Path, should_autosave: bool, should_overwrite: bool
    ):
        """
        Prompts a file explorer to determine the file to save the
        generated transcription.
        """
        file_dir = file_path.parent
        txt_file_name = f"{file_path.stem}.txt"

        if should_autosave:
            txt_file_path = file_path.parent / txt_file_name
        else:
            txt_file_path = filedialog.asksaveasfilename(
                initialdir=file_dir,
                initialfile=txt_file_name,
                title=_("Save as"),
                defaultextension=".txt",
                filetypes=[(_("Text file"), "*.txt"), (_("All Files"), "*.*")],
            )

        if not txt_file_path:
            return

        if should_overwrite or not os.path.exists(txt_file_path):
            with open(txt_file_path, "w") as txt_file:
                txt_file.write(self.transcription.text)

        if self.transcription.should_subtitle:
            self._generate_subtitles(Path(txt_file_path), should_overwrite)

    # PRIVATE METHODS

    def _prepare_for_file_transcription(self, source_file_path: Path):
        if self._is_file_valid(source_file_path):
            self.transcription.source_path = source_file_path

            threading.Thread(
                target=lambda loop: loop.run_until_complete(
                    self._handle_transcription_process()
                ),
                args=(asyncio.new_event_loop(),),
            ).start()
        else:
            raise ValueError("Error: No valid file selected.")

    def _prepare_for_directory_files_transcription(self, dir_path: Path):
        self.transcription.source_path = dir_path

        threading.Thread(
            target=lambda loop: loop.run_until_complete(
                self._handle_transcription_process()
            ),
            args=(asyncio.new_event_loop(),),
        ).start()

    def _prepare_for_mic_transcription(self):
        threading.Thread(target=self._record_from_mic).start()

    def _prepare_for_yt_transcription(self):
        threading.Thread(target=self._download_audio_from_yt_video).start()

    def _handle_exception(self, e: Exception):
        print(traceback.format_exc())
        self.view.on_processed_transcription(success=False)
        self.view.display_text(repr(e))

    @staticmethod
    def _is_file_valid(file_path: Path) -> bool:
        is_audio = file_path.suffix in c.AUDIO_FILE_EXTENSIONS
        is_video = file_path.suffix in c.VIDEO_FILE_EXTENSIONS

        return file_path.is_file() and (is_audio or is_video)

    @staticmethod
    def _get_transcribable_files_from_dir(dir_path: Path) -> list[Path]:
        matching_extensions = c.AUDIO_FILE_EXTENSIONS + c.VIDEO_FILE_EXTENSIONS
        matching_files = []
        for root, _, files in os.walk(dir_path):
            for file in files:
                if any(file.endswith(ext) for ext in matching_extensions):
                    matching_files.append(Path(root) / file)

        return matching_files

    async def _transcribe_using_whisperx(self, file_path: Path):
        config_whisperx = cm.ConfigManager.get_config_whisperx()

        device = "cpu" if config_whisperx.use_cpu else "cuda"
        task = "translate" if self.transcription.should_translate else "transcribe"

        try:
            model = whisperx.load_model(
                config_whisperx.model_size,
                device,
                compute_type=config_whisperx.compute_type,
                task=task,
                language=self.transcription.language_code,
            )

            audio_path = str(file_path)
            audio = whisperx.load_audio(audio_path)
            self._whisperx_result = model.transcribe(
                audio, batch_size=config_whisperx.batch_size
            )

            text_combined = " ".join(
                segment["text"].strip() for segment in self._whisperx_result["segments"]
            )

            # Align output if should subtitle
            if self.transcription.should_subtitle:
                model_aligned, metadata = whisperx.load_align_model(
                    language_code=self.transcription.language_code, device=device
                )
                self._whisperx_result = whisperx.align(
                    self._whisperx_result["segments"],
                    model_aligned,
                    metadata,
                    audio,
                    device,
                    return_char_alignments=False,
                )

            self.transcription.text = text_combined

            if self.transcription.source != AudioSource.DIRECTORY:
                self.view.display_text(self.transcription.text)

        except Exception as e:
            self._handle_exception(e)

    async def _transcribe_using_google_api(self, file_path: Path):
        """
        Splits a large audio file into chunks
        and applies speech recognition on each one.
        """
        # Can be the transcription or an error text
        transcription_text = ""

        # Create a directory to store the audio chunks
        chunks_directory = ROOT_PATH / "audio-chunks"
        chunks_directory.mkdir(exist_ok=True)

        try:
            # Get file extension
            content_type = Path(file_path).suffix

            sound = None
            # Open the audio file using pydub
            if content_type in c.AUDIO_FILE_EXTENSIONS:
                sound = AudioSegment.from_file(file_path)

            elif content_type in c.VIDEO_FILE_EXTENSIONS:
                clip = VideoFileClip(str(file_path))
                video_audio_path = chunks_directory / f"{Path(file_path).stem}.wav"
                clip.audio.write_audiofile(video_audio_path)
                sound = AudioSegment.from_wav(video_audio_path)

            audio_chunks = split_on_silence(
                sound,
                # Minimum duration of silence required to consider a segment as a split point
                min_silence_len=500,
                # Audio with a level -X decibels below the original audio level will be considered as silence
                silence_thresh=sound.dBFS - 40,
                # Adds a buffer of silence before and after each split point
                keep_silence=100,
            )

            # Create a speech recognition object
            r = sr.Recognizer()

            # Get Google API key (if any)
            config_google_api = cm.ConfigManager.get_config_google_api()
            api_key = config_google_api.api_key or None

            # Process each chunk
            for idx, audio_chunk in enumerate(audio_chunks):
                # Export audio chunk and save it in the `chunks_directory` directory.
                chunk_filename = os.path.join(chunks_directory, f"chunk{idx}.wav")
                audio_chunk.export(chunk_filename, bitrate="192k", format="wav")

                # Recognize the chunk
                with sr.AudioFile(chunk_filename) as source:
                    r.adjust_for_ambient_noise(source)
                    audio_listened = r.record(source)

                    try:
                        # Try converting it to text
                        chunk_text = r.recognize_google(
                            audio_listened,
                            language=self.transcription.language_code,
                            key=api_key,
                        )

                        chunk_text = f"{chunk_text.capitalize()}. "
                        transcription_text += chunk_text
                        print(f"chunk text: {chunk_text}")

                    except Exception:
                        continue

            self.transcription.text = transcription_text

        except Exception:
            self.view.display_text(traceback.format_exc())

        finally:
            # Delete temporal directory and files
            shutil.rmtree(chunks_directory)

            is_not_dir = self.transcription.source != AudioSource.DIRECTORY
            if self.transcription.text and is_not_dir:
                self.view.display_text(self.transcription.text)

    def _record_from_mic(self):
        self._is_mic_recording = True
        audio_data = []

        try:
            r = sr.Recognizer()

            with sr.Microphone() as mic:
                while self._is_mic_recording:
                    audio_chunk = r.listen(mic, timeout=5)
                    audio_data.append(audio_chunk)

            if audio_data:
                filename = "mic-output.wav"
                au.save_audio_data(audio_data, filename=filename)
                self.transcription.source_path = Path(filename)

                threading.Thread(
                    target=lambda loop: loop.run_until_complete(
                        self._handle_transcription_process()
                    ),
                    args=(asyncio.new_event_loop(),),
                ).start()
            else:
                e = ValueError("No audio detected")
                self._handle_exception(e)

        except Exception as e:
            self.view.stop_recording_from_mic()
            self._handle_exception(e)

    def _generate_subtitles(self, file_path: Path, should_overwrite: bool):
        config_subtitles = cm.ConfigManager.get_config_subtitles()

        output_formats = ["srt", "vtt"]
        output_dir = file_path.parent

        for output_format in output_formats:
            path_to_check = file_path.parent / f"{file_path.stem}.{output_format}"

            if should_overwrite or not os.path.exists(path_to_check):
                writer = whisperx.transcribe.get_writer(output_format, str(output_dir))
                writer_args = {
                    "highlight_words": config_subtitles.highlight_words,
                    "max_line_count": config_subtitles.max_line_count,
                    "max_line_width": config_subtitles.max_line_width,
                }

                # https://github.com/m-bain/whisperX/issues/455#issuecomment-1707547704
                self._whisperx_result["language"] = "en"

                writer(self._whisperx_result, file_path, writer_args)

    def _download_audio_from_yt_video(self):
        try:
            yt = YouTube(self.transcription.youtube_url)
            stream = yt.streams.filter(only_audio=True).first()
            output_file = stream.download(output_path=".", filename="yt-audio.mp3")

            if output_file:
                self.transcription.source_path = Path(output_file)

                threading.Thread(
                    target=lambda loop: loop.run_until_complete(
                        self._handle_transcription_process()
                    ),
                    args=(asyncio.new_event_loop(),),
                ).start()

        except RegexMatchError:
            e = ValueError("The URL is not correct.")
            self._handle_exception(e)

        except Exception as e:
            self._handle_exception(e)
