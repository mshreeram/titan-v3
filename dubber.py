from pydub import AudioSegment
from google.cloud import texttospeech
from google.cloud import translate_v2 as translate
from moviepy.editor import VideoFileClip, AudioFileClip, CompositeVideoClip
from moviepy.video.tools.subtitles import SubtitlesClip, TextClip
from typing import NamedTuple, List, Optional, Sequence
import os
import shutil
import ffmpeg
import time
import sys
import tempfile
from dotenv import load_dotenv
import html
import pvleopard
load_dotenv()

leopard = pvleopard.create(access_key=os.environ["PV_ACCESS_KEY"])

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
    translate_client = translate.Client()
    result = translate_client.translate(input, target_language=targetLang, source_language="en")

    return result['translatedText']


def speak(text, languageCode, voiceName=None, speakingRate=1):
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    if not voiceName:
        voice = texttospeech.VoiceSelectionParams(
            language_code=languageCode, ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL
        )
    else:
        voice = texttospeech.VoiceSelectionParams(
            language_code=languageCode, name=voiceName
        )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=speakingRate
    )
    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )
    return response.audio_content


def text_to_speech(text, languageCode, durationSecs, voiceName=None):
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
    currentDuration = find_duration(baseAudio)
    for i in range(2):
        currentDuration = find_duration(baseAudio)
        print(currentDuration, durationSecs)

        if abs(currentDuration - durationSecs) < 0.5:
            break

        ratio = currentDuration / durationSecs
        ratio = min(max(ratio, min_rate), max_rate)

        baseAudio = speak(text, languageCode, voiceName, speakingRate=ratio)
    
    return baseAudio, (durationSecs - currentDuration) if durationSecs > currentDuration else 0, currentDuration

def merge_audio(startPostions, audioDir, videoPath, outputPath, lags, currentDurations):
    audioFiles = os.listdir(audioDir)
    audioFiles.sort(key=lambda x: int(x.split('.')[0]))

    segments = [AudioSegment.from_mp3(os.path.join(audioDir, x)) for x in audioFiles]
    dubbed = AudioSegment.from_file(videoPath)

    emptySegment = AudioSegment.from_mp3("static/empty-audio.mp3")

    for position, segment, lag, duration in zip(startPostions, segments, lags, currentDurations):
        dubbed = dubbed.overlay(segment, position=position * 1000, gain_during_overlay= -50)
        if lag != 0:
            emptyLag = emptySegment[:lag * 1000]
            dubbed = dubbed.overlay(emptyLag, position=position+duration, gain_during_overlay = -50)
        

    audioFile = tempfile.NamedTemporaryFile()
    dubbed.export(audioFile)
    audioFile.flush()

    clip = VideoFileClip(videoPath)
    audio = AudioFileClip(audioFile.name)
    clip = clip.set_audio(audio)

    clip.write_videofile(outputPath, codec='libx264', audio_codec='aac')
    audioFile.close()

def dub(videoPath, outputDir, srcLang, targetLangs=[], speakerCount=1, voices={}, genAudio=False):

    videoName = os.path.split(videoPath)[-1].split('.')[0]
    if not os.path.exists(outputDir):
        os.mkdir(outputDir)

    outputFiles = os.listdir(outputDir)

    if not f"{videoName}.mp3" in outputFiles:
        print("Extracting audio from video")
        outputAudioPath = f"{outputDir}/{videoName}.mp3"
        extract_audio(videoPath, outputAudioPath)

    sentences = []
    startPositions = []
    durations = []
    lags = []
    currentDurations = []

    if not "transcript.srt" in outputFiles:
        outputSrtPath = f"{outputDir}/transcript.srt"
        audioFilePath = f"{outputDir}/{videoName}.mp3"
        speech_to_text(audioFilePath, outputSrtPath)

        sentences, startPositions, durations = extract_sentences_from_srt(outputSrtPath)
    
    translatedSentences = {}
    for lang in targetLangs:
        print(f"Translating the text")
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
        
        for i, sentence in enumerate(translatedSentences[lang]):
            voiceName = voices[lang] if lang in voices else None
            audio, lag, currentDuration = text_to_speech(sentence, lang, durations[i], voiceName=voiceName)

            lags.append(lag)
            currentDurations.append(currentDuration)

            with open(f"{languageDir}/{i}.mp3", 'wb') as f: 
                f.write(audio)

    dubbedDir = f"{outputDir}/dubbedVideos" 

    if not "dubbedVideos" in outputFiles:
        os.mkdir(dubbedDir)

    for lang in targetLangs:
        print(f"merging generated audio with video")
        outFile = f"{dubbedDir}/{videoName}[{lang}].mp4"
        merge_audio(startPositions, f"{audioDir}/{lang}", videoPath, outFile, lags, currentDurations) 

    print("Done")