from pydub import AudioSegment
from google.cloud import speech_v1p1beta1 as speech
from google.cloud import texttospeech
from google.cloud import translate_v2 as translate
from google.cloud import storage
from moviepy.editor import VideoFileClip, AudioFileClip, CompositeVideoClip
from moviepy.video.tools.subtitles import SubtitlesClip, TextClip
from typing import NamedTuple, List, Optional, Sequence
import os
import shutil
import ffmpeg
import time
import json
import sys
import tempfile
import uuid
from dotenv import load_dotenv
import fire
import html
import pvleopard
leopard = pvleopard.create(access_key="lLqy5p+LTPk9vZDhQo0w9EwO++1DiUBCmlXqWkUnwyPsT3g1iej99A==")


# Load config in .env file
load_dotenv()

def extract_audio(videoPath, outputPath):
    video = VideoFileClip(videoPath)
    video.audio.write_audiofile(outputPath)

def speech_to_text(audioFilePath, srtFilePath):
    transcript, words = leopard.process_file(audioFilePath)
    print(transcript)
    print(words)

    def second_to_timecode(x: float) -> str:
        hour, x = divmod(x, 3600)
        minute, x = divmod(x, 60)
        second, x = divmod(x, 1)
        millisecond = int(x * 1000.)
        return '%.2d:%.2d:%.2d,%.3d' % (hour, minute, second, millisecond)

    def to_srt(
            words: Sequence[pvleopard.Leopard.Word],
            endpoint_sec: float = 1.,
            length_limit: Optional[int] = 16) -> str:

        def _helper(end: int) -> None:
            lines.append("%d" % section)
            lines.append(
                "%s --> %s" %
                (
                    second_to_timecode(words[start].start_sec),
                    second_to_timecode(words[end].end_sec)
                )
            )
            lines.append(' '.join(x.word for x in words[start:(end + 1)]))
            lines.append('')


        lines = list()
        section = 0
        start = 0
        for k in range(1, len(words)):
            if ((words[k].start_sec - words[k - 1].end_sec) >= endpoint_sec) or \
                    (length_limit is not None and (k - start) >= length_limit):
                _helper(k - 1)
                start = k
                section += 1
        _helper(len(words) - 1)

        return '\n'.join(lines)
    
    with open(srtFilePath, 'w') as f:
        f.write(to_srt(words))

def extract_sentences_from_srt(srtFilePath):
    sentences = []
    audio_splits = []
    durations = []

    def convert_to_sec(timeFormat):
      temp = timeFormat.split(',')
      micro_sec = int(temp[1]) * 0.001
      time = temp[0].split(':')
      seconds = int(time[0]) * 60 * 60 + int(time[1]) * 60 + int(time[2])
      return seconds + micro_sec

    with open(srtFilePath, 'r') as srt_file:
        lines = srt_file.readlines()

        current_sentence = ""

        for line in lines:
            line = line.strip()
            if '-->' in line:
              start_time = convert_to_sec(line[0:12])
              #audio_splits.append(start_time)
              end_time = convert_to_sec(line[18:])

              audio_splits.append(start_time)
              durations.append(end_time - start_time)


            if not line:
                # Empty line indicates the end of a subtitle
                if current_sentence:
                    sentences.append(current_sentence)
                    current_sentence = ""
            elif not line.isdigit() and '-->' not in line:
                # Skip line numbers and timing lines
                current_sentence += "" + line

        # Add the last sentence if there is any
        if current_sentence:
            sentences.append(current_sentence)

    return sentences, audio_splits, durations

def translate_text(input, targetLang):
    """Translates from sourceLang to targetLang. If sourceLang is empty,
    it will be auto-detected.

    Args:
        sentence (String): Sentence to translate
        targetLang (String): i.e. "en"
        sourceLang (String, optional): i.e. "es" Defaults to None.

    Returns:
        String: translated text
    """

    translate_client = translate.Client()
    result = translate_client.translate(
        input, target_language=targetLang, source_language="en")

    return result['translatedText']


def speak(text, languageCode, voiceName=None, speakingRate=1):
    """Converts text to audio

    Args:
        text (String): Text to be spoken
        languageCode (String): Language (i.e. "en")
        voiceName: (String, optional): See https://cloud.google.com/text-to-speech/docs/voices
        speakingRate: (int, optional): speed up or slow down speaking
    Returns:
        bytes : Audio in wav format
    """

    # Instantiates a client
    client = texttospeech.TextToSpeechClient()

    # Set the text input to be synthesized
    synthesis_input = texttospeech.SynthesisInput(text=text)

    # Build the voice request, select the language code ("en-US") and the ssml
    # voice gender ("neutral")
    if not voiceName:
        voice = texttospeech.VoiceSelectionParams(
            language_code=languageCode, ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL
        )
    else:
        voice = texttospeech.VoiceSelectionParams(
            language_code=languageCode, name=voiceName
        )

    # Select the type of audio file you want returned
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=speakingRate
    )

    # Perform the text-to-speech request on the text input with the selected
    # voice parameters and audio file type
    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )

    return response.audio_content


def text_to_speech(text, languageCode, durationSecs, voiceName=None):
    """Speak text within a certain time limit.
    If audio already fits within duratinSecs, no changes will be made.

    Args:
        text (String): Text to be spoken
        languageCode (String): language code, i.e. "en"
        durationSecs (int): Time limit in seconds
        voiceName (String, optional): See https://cloud.google.com/text-to-speech/docs/voices

    Returns:
        bytes : Audio in wav format
    """
    def find_duration(audio):
        file = tempfile.NamedTemporaryFile(mode="w+b")
        file.write(audio)
        file.flush()
        duration = AudioSegment.from_mp3(file.name).duration_seconds
        file.close()
        return duration

    baseAudio = speak(text, languageCode, voiceName=voiceName)
    assert len(baseAudio)

    min_rate, max_rate = 0.1, 4

    for i in range(2):
        currentDuration = find_duration(baseAudio)
        print(currentDuration, durationSecs)

        if abs(currentDuration - durationSecs) < 0.5:
            break

        ratio = currentDuration / durationSecs
        ratio = min(max(ratio, min_rate), max_rate)

        baseAudio = speak(text, languageCode, voiceName, speakingRate=ratio)
    
    return baseAudio

def merge_audio(startPostions, audioDir, videoPath, outputPath):
  audioFiles = os.listdir(audioDir)
  audioFiles.sort(key=lambda x: int(x.split('.')[0]))

  segments = [AudioSegment.from_mp3(os.path.join(audioDir, x)) for x in audioFiles]
    # Also, grab the original audio
  dubbed = AudioSegment.from_file(videoPath)

  for position, segment in zip(startPostions, segments):
    dubbed = dubbed.overlay(segment, position=position * 1000, gain_during_overlay= -30)

  audioFile = tempfile.NamedTemporaryFile()
  dubbed.export(audioFile)
  audioFile.flush()

  # Add the new audio to the video and save it
  clip = VideoFileClip(videoPath)
  audio = AudioFileClip(audioFile.name)
  clip = clip.set_audio(audio)

  clip.write_videofile(outputPath, codec='libx264', audio_codec='aac')
  audioFile.close()

def dub(
        videoPath, outputDir, srcLang, targetLangs=[],
        storageBucket=None, phraseHints=[],
        speakerCount=1, voices={}, genAudio=False):
    """Translate and dub a movie.

    Args:
        videoPath (String): File to dub
        outputDir (String): Directory to write output files
        srcLang (String): Language code to translate from (i.e. "fi")
        targetLangs (list, optional): Languages to translate too, i.e. ["en", "fr"]
        storageBucket (String, optional): GCS bucket for temporary file storage. Defaults to None.
        phraseHints (list, optional): "Hints" for words likely to appear in audio. Defaults to [].
        dubSrc (bool, optional): Whether to generate dubs in the source language. Defaults to False.
        speakerCount (int, optional): How many speakers in the video. Defaults to 1.
        voices (dict, optional): Which voices to use for dubbing, i.e. {"en": "en-AU-Standard-A"}. Defaults to {}.
        srt (bool, optional): Path of SRT transcript file, if it exists. Defaults to False.
        newDir (bool, optional): Whether to start dubbing from scratch or use files in outputDir. Defaults to False.
        genAudio (bool, optional): Generate new audio, even if it's already been generated. Defaults to False.
        noTranslate (bool, optional): Don't translate. Defaults to False.

    Raises:
        void : Writes dubbed video and intermediate files to outputDir
    """

    videoName = os.path.split(videoPath)[-1].split('.')[0]

    if not os.path.exists(outputDir):
        os.mkdir(outputDir)

    outputFiles = os.listdir(outputDir)

    if not f"{videoName}.mp3" in outputFiles:
        print("Extracting audio from video")
        outputAudioPath = f"{outputDir}/{videoName}.mp3"
        extract_audio(videoPath, outputAudioPath)
        print(f"Wrote {outputAudioPath}")

    sentences = []
    startPositions = []
    durations = []

    if not "transcript.srt" in outputFiles:
        outputSrtPath = f"{outputDir}/transcript.srt"
        audioFilePath = f"{outputDir}/{videoName}.mp3"
        speech_to_text(audioFilePath, outputSrtPath)

        sentences, startPositions, durations = extract_sentences_from_srt(outputSrtPath)
    
    translatedSentences = {}
    for lang in targetLangs:
        print(f"Translating to {lang}")
        translatedSentences[lang] = []
        for sentence in sentences:
            translatedSentences[lang].append(translate_text(sentence, lang))

    audioDir = f"{outputDir}/audioClips"
    if not "audioClips" in outputFiles:
        os.mkdir(audioDir)

    for lang in targetLangs:
        languageDir = f"{audioDir}/{lang}"
        if os.path.exists(languageDir):
            shutil.rmtree(languageDir)
        os.mkdir(languageDir)
        print(f"Synthesizing audio for {lang}")
        
        for i, sentence in enumerate(translatedSentences[lang]):
            voiceName = voices[lang] if lang in voices else None
            audio = text_to_speech(sentence, lang, durations[i], voiceName=voiceName)

            with open(f"{languageDir}/{i}.mp3", 'wb') as f: 
                f.write(audio)

    dubbedDir = f"{outputDir}/dubbedVideos" 

    if not "dubbedVideos" in outputFiles:
        os.mkdir(dubbedDir)

    for lang in targetLangs:
        print(f"Dubbing audio for {lang}")
        outFile = f"{dubbedDir}/{videoName}[{lang}].mp4"
        merge_audio(startPositions, f"{audioDir}/{lang}", videoPath, outFile) 

    print("Done")