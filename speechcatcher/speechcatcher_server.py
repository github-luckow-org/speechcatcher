# Speechcatcher streaming server interface. Decode speech with speechcatcher streaming models using live data from websockets.
# 2024, Dr. Benjamin Milde
#
# Note: work in progress!

import asyncio
import websockets
import numpy as np
import argparse
import subprocess
import ffmpeg
from scipy.io import wavfile
from io import BytesIO
from threading import Thread, Lock
from queue import Queue, Empty
from speechcatcher.speechcatcher import (
    load_model, tags, progress_output, upperCaseFirstLetter,
    is_completed, ensure_dir, segment_speech, Speech2TextStreaming
)
import json

debug_wav_path = "debug.wav"

# This class models the lifetime of a client ASR session
class SpeechRecognitionSession:
    def __init__(self, speech2text, audio_format="webm", finalize_update_iters=7, vosk_output_format=False):
        self.speech2text = speech2text
        self.finalize_update_iters = finalize_update_iters
        self.n_best_lens = []
        self.prev_lines = 0
        self.blocks = []
        self.audio_format = audio_format
        self.vosk_output_format = vosk_output_format
        self.process = None
        self.stdout_queue = Queue()
        self.stderr_queue = Queue()
        self.start_ffmpeg_process()
        self.blocks = []
        self.write_debug_wav = 10

    # Each session runs an ffmpeg stream with pipes to convert the input audio.
    # The ffmpeg process runs through the entire lifetime of a session.
    def start_ffmpeg_process(self, debug=False):
        command = [
            "ffmpeg", "-loglevel", "debug" if debug else "info", "-f", self.audio_format, "-i", "pipe:0",
            "-f", "wav", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", "pipe:1"
        ]
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10**7 #10mb buffer
        )
        Thread(target=self.read_from_ffmpeg_stdout, daemon=True).start()
        Thread(target=self.read_from_ffmpeg_stderr, daemon=True).start()

    def read_from_ffmpeg_stdout(self):
        while True:
            output = self.process.stdout.read(4096)
            if output:
                self.stdout_queue.put(output)

    def read_from_ffmpeg_stderr(self):
        while True:
            error = self.process.stderr.readline()
            if error:
                print("FFmpeg stderr:", error.decode().strip())

    def decode_audio(self, audio_chunk):
        if self.process is None:
            self.start_ffmpeg_process()

        self.process.stdin.write(audio_chunk)
        # Ensure data is always flushed through FFmpeg
        self.process.stdin.flush()
        data = b''

        try:
            while not self.stdout_queue.empty():
                data += self.stdout_queue.get_nowait()
        except Empty:
            pass

        return np.frombuffer(data, dtype='int16')

    # This function processes one chunk of incoming audio
    def process_audio_chunk(self, audio_chunk, is_final=False, save_debug_wav=False, debug=False):
        print("Incoming audio data len:", len(audio_chunk), type(audio_chunk))

        data = self.decode_audio(audio_chunk)

        if data.size == 0:
            if debug:
                print("data was zero:", data)
            return ""

        # Only for debug purposes, save the incoming and converted audio as wav.
        if save_debug_wav:
            self.blocks.append(data)
            if len(self.blocks) > self.write_debug_wav:
                samplerate = 16000    
                print("\nSaving debug output...")
                wavfile.write(debug_wav_path, samplerate, np.concatenate(self.blocks, axis=None))
                
                self.write_debug_wav += 10

        data = data.astype(np.float16) / 32767.0  # Normalize

        if debug:
            print("data after decode audio is:", data)
            print("is final:", is_final)

        # Process final results
        if is_final:
            results = self.speech2text(speech=data, is_final=True)
            nbests0 = results[0][0]
            if len(nbests0) >= 1:
                if nbests0[-1] != '.' and nbests0[-1] != '!' and nbests0[-1] != '?':
                    nbests0 += '.'
                nbests0 += '\n'
            if self.vosk_output_format:
                return self.format_vosk_result(results)
            return nbests0

        # Process partial results
        if len(self.n_best_lens) < self.finalize_update_iters:
            finalize_iteration = False
        else:
            if all(x == self.n_best_lens[-1] for x in self.n_best_lens[-10:]):
                finalize_iteration = True
                self.n_best_lens = []
            else:
                finalize_iteration = False

        results = self.speech2text(speech=data, is_final=finalize_iteration)

        if debug:
            print("results:", results)

        if results is not None and len(results) > 0:
            nbests0 = results[0][0]
            if self.vosk_output_format:
                return self.format_vosk_partial(nbests0)
            if finalize_iteration:
                if len(nbests0) >= 1:
                    if nbests0[-1] != '.' and nbests0[-1] != '!' and nbests0[-1] != '?':
                        nbests0 += '.'
                    nbests0 += '\n'
            else:
                nbest_len = len(nbests0)
                self.n_best_lens.append(nbest_len)
            return nbests0
        return ""

    # Helper function to format partial results in Vosk style
    def format_vosk_partial(self, partial_text):
        return {
            "partial": partial_text
        }

    # Helper function to format final results in Vosk style
    def format_vosk_result(self, results):
        words = []
        text = ""
        for token, timestamp in zip(results[0][1], results[0][2]):
            word_info = {
                "conf": 1.0,  # Assuming full confidence as Speechcatcher doesn't output confidence scores per token
                "start": timestamp - 0.1,  # Approximation, adjust as needed
                "end": timestamp,
                "word": token
            }
            words.append(word_info)
            text += token + " "

        return {
            "result": words,
            "text": text.strip()
        }

# This class loads a pool of models that can be used by new client connections
class Speech2TextPool:
    def __init__(self, model_tag, device, beam_size, cache_dir, pool_size):
        self.pool_size = pool_size
        self.model_tag = model_tag
        self.device = device
        self.beam_size = beam_size
        self.cache_dir = cache_dir
        self.pool = Queue(maxsize=pool_size)
        self.lock = Lock()

        i=1
        # Preload the models
        for _ in range(pool_size):
            print("Load instance:",i)
            model = load_model(tag=model_tag, device=device, beam_size=beam_size, cache_dir=cache_dir)
            self.pool.put(model)
            i+=1

    def acquire(self):
        with self.lock:
            if self.pool.empty():
                return None
            return self.pool.get()

    def release(self, model):
        with self.lock:
            self.pool.put(model)

async def recognize_ws(websocket, path, model_pool, audio_format, vosk_output_format):
    print("Client connected")
    speech2text = model_pool.acquire()
    if speech2text is None:
        await websocket.send("Server busy, please try again later.")
        await websocket.close()
        return

    session = SpeechRecognitionSession(speech2text, audio_format, vosk_output_format=vosk_output_format)
    try:
        async for message in websocket:
            transcription = session.process_audio_chunk(message)
            if transcription:
                if vosk_output_format:
                    await websocket.send(json.dumps(transcription))
                else:
                    await websocket.send(str(transcription))
    except websockets.exceptions.ConnectionClosed:
        print("Client disconnected")
    finally:
        model_pool.release(speech2text)

async def start_server(host, port, model_pool, audio_format, vosk_output_format):
    server = await websockets.serve(lambda ws, path: recognize_ws(ws, path, model_pool, audio_format, vosk_output_format), host, port)
    await server.wait_closed()

def main():
    parser = argparse.ArgumentParser(description="Speechcatcher WebSocket Server for streaming ASR")
    parser.add_argument('--host', type=str, default='localhost', help='Host for the WebSocket server')
    parser.add_argument('--port', type=int, default=8765, help='Port for the WebSocket server')
    parser.add_argument('--model', type=str, default='de_streaming_transformer_xl', choices=tags.keys(),
                        help='Model to use for ASR')
    parser.add_argument('--device', type=str, default='cpu', choices=['cpu', 'cuda'],
                        help="Device to run the ASR model on ('cpu' or 'cuda')")
    parser.add_argument('--beamsize', type=int, default=5, help='Beam size for the decoder')
    parser.add_argument('--cache-dir', type=str, default='~/.cache/espnet', help='Directory for model cache')
    parser.add_argument('--format', type=str, default='webm', choices=['wav', 'mp3', 'mp4', 'pcm', 'webm', 'ogg', 'acc'],
                        help='Audio format for the input stream')
    parser.add_argument('--pool-size', type=int, default=5, help='Number of speech2text instances to preload')
    parser.add_argument('--vosk-output-format', action='store_true', help='Enable Vosk-like output format')

    args = parser.parse_args()

    if args.model not in tags:
        print(f"Model {args.model} is not a valid model!")
        print("Options are:", ", ".join(tags.keys()))
        exit(1)

    tag = tags[args.model]
    print(f'Loading model pool: {tag}')

    model_pool = Speech2TextPool(model_tag=tag, device=args.device, beam_size=args.beamsize, cache_dir=args.cache_dir, pool_size=args.pool_size)

    print(f'Starting WebSocket server on ws://{args.host}:{args.port}')
    asyncio.get_event_loop().run_until_complete(start_server(args.host, args.port, model_pool, args.format, args.vosk_output_format))
    asyncio.get_event_loop().run_forever()

if __name__ == '__main__':
    main()
