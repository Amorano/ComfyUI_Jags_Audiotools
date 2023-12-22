# Imports

import subprocess, sys, os
import torch
import random
from pathlib import Path 
import folder_paths
import importlib
import yaml
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from server import PromptServer
from aiohttp import web
from folder_paths import models_dir, get_filename_list
from comfy.model_management import get_torch_device
from scipy.fft import fft
from pydub import AudioSegment

def get_comfy_dir():
    dirs = __file__.split('\\')
    comfy_index = None
    for i, dir in enumerate(dirs):
        if dir == "ComfyUI":
            comfy_index = i
            break
    if comfy_index is not None:
        # Join the list up to the "ComfyUI" folder
        return '\\'.join(dirs[:comfy_index+1])
    else:
        return None

comfy_dir = get_comfy_dir()

def check_import(importname, installname=None):
    try:
        importlib.import_module(importname)
    except ModuleNotFoundError:
        installname = installname if installname else importname
        print(f"Required module '{importname}' not found. Please install it using 'pip install {installname}'.")

check_import("audio_diffusion_pytorch")
check_import("diffusion", "v-diffusion-pytorch")
check_import("k_diffusion", "k-diffusion")
check_import("soundfile")
check_import("torchaudio")

import soundfile as sf
import torchaudio

PromptServer.instance.app._client_max_size = 250 * 1024 * 1024 #  250 MB

# Add route for uploading audio, duplicates image upload but to audio_input
@PromptServer.instance.routes.post("/ComfyUI_Jags_Audiotools/upload/audio")
async def upload_audio(request):
    upload_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "audio_input")

    if not os.path.exists(upload_dir):
        os.makedirs(upload_dir)
    
    post = await request.post()
    file = post.get("file")

    if file and file.file:
        filename = file.filename
        if not filename:
            return web.Response(status=400)

        if os.path.exists(os.path.join(upload_dir, filename)):
            os.remove(os.path.join(upload_dir, filename))

        filepath = os.path.join(upload_dir, filename)

        with open(filepath, "wb") as f:
            f.write(file.file.read())
        
        return web.json_response({"name" : filename})
    else:
        return web.Response(status=400)

# Add route for getting audio, duplicates view image but allows audio_input
@PromptServer.instance.routes.get("/ComfyUI_Jags_Audiotools/audio")
async def view_image(request):
    if "filename" in request.rel_url.query:
        type = request.rel_url.query.get("type", "audio_input")
        if type not in ["output", "input", "temp", "audio_input"]:
            return web.Response(status=400)

        output_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), type)
        if "subfolder" in request.rel_url.query:
            full_output_dir = os.path.join(output_dir, request.rel_url.query["subfolder"])
            if os.path.commonpath((os.path.abspath(full_output_dir), output_dir)) != output_dir:
                return web.Response(status=403)
            output_dir = full_output_dir

        filename = request.rel_url.query["filename"]
        filename = os.path.basename(filename)
        file = os.path.join(output_dir, filename)

        if os.path.isfile(file):
            return web.FileResponse(file, headers={"Content-Disposition": f"filename=\"{filename}\""})
        
    return web.Response(status=404)


config = os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.yaml")


if not os.path.exists(config):
    with open(config, "w") as f:
        yaml.dump({"model_folder": f"{os.path.join(models_dir, 'audio_diffusion')}"}, f)

with open(config, "r") as f:
    config = yaml.safe_load(f)
models_folder = config["model_folder"]

# init and sample_diffusion lib load


libs = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "libs")   
#if not os.path.exists(os.path.join(comfy_dir, 'custom_nodes/ComfyUI_Jags_Audiotools/libs')):
    #os.makedirs(os.path.join(comfy_dir, 'custom_nodes/ComfyUI_Jags_Audiotools/libs'))
#libs = os.path.join(comfy_dir, 'custom_nodes/ComfyUI_Jags_Audiotools/libs') 
#if not os.path.exists(os.path.join(comfy_dir, libs)):
#    os.system (os.path.join(comfy_dir, libs))
#sys.path.append(os.path.join(comfy_dir, libs ))

from libs.util.util import load_audio, crop_audio
from libs.dance_diffusion.api import RequestHandler, Request, ModelType
from libs.diffusion_library.sampler import SamplerType
from libs.diffusion_library.scheduler import SchedulerType
from libs.dance_diffusion.dd.model import DDModelWrapper
from libs.dance_diffusion.dd.inference import DDInference

def save_audio(audio_out, output_path: str, sample_rate, id_str:str = None):
    out_files = []
    if not os.path.exists(output_path):
        os.makedirs(output_path)

    ix = 1
    for sample in audio_out:
        while True:
            output_file = os.path.join(output_path, f"sample_{id_str}_{ix}.wav" if id_str else f"sample_{ix}.wav")
            if not os.path.exists(output_file):
                break
            ix += 1
        
        open(output_file, "a").close()
        
        output = sample.cpu()
        torchaudio.save(output_file, output, sample_rate)
        out_files.append(output_file)
        ix += 1
    
    return out_files

# sound play functionality for audio nodes
# needs further testing
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "hide"
try:
    from pygame import mixer
except ModuleNotFoundError:
    # install pixelsort in current venv
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pygame"])
    from pygame import mixer

mixer.init()

def PlaySound(path, volume):
    mixer.music.load(path)
    mixer.music.set_volume(volume)
    mixer.music.play()

# ****************************************************************************
#testing the audio file for playback

# ****************************************************************************
# *                                   NODES                                  *
# ****************************************************************************
class AudioData:
    def __init__(self, audio_file) -> None:
        
        # Extract the sample rate
        sample_rate = audio_file.frame_rate

        # Get the number of audio channels
        num_channels = audio_file.channels

        # Extract the audio data as a NumPy array
        audio_data = np.array(audio_file.get_array_of_samples())
        self.audio_data = audio_data
        self.sample_rate = sample_rate
        self.num_channels = num_channels
    
    def get_channel_audio_data(self, channel: int):
        if channel < 0 or channel >= self.num_channels:
            raise IndexError(f"Channel '{channel}' out of range. total channels is '{self.num_channels}'.")
        return self.audio_data[channel::self.num_channels]
    
    def get_channel_fft(self, channel: int):
        audio_data = self.get_channel_audio_data(channel)
        return fft(audio_data)

class AudioFFTData:
    def __init__(self, audio_data, sample_rate) -> None:

        self.fft = fft(audio_data)
        self.length = len(self.fft)
        self.frequency_bins = np.fft.fftfreq(self.length, 1 / sample_rate)
    
    def get_max_amplitude(self):
        return np.max(np.abs(self.fft))
    
    def get_normalized_fft(self) -> float:
        max_amplitude = self.get_max_amplitude()
        return np.abs(self.fft) / max_amplitude

    def get_indices_for_frequency_bands(self, lower_band_range: int, upper_band_range: int):
        return np.where((self.frequency_bins >= lower_band_range) & (self.frequency_bins < upper_band_range))

    def __len__(self):
        return self.length



# ****************************************************************************
class AudioInference():
    def __init__(self):
        pass
    
    @classmethod
    def INPUT_TYPES(cls):
        """
        Input Types
        """
        return {
            "required": {
                "audio_model": ("DD_MODEL", ),
                "mode": (['Generation', 'Variation'],),
                "batch_size": ("INT", {"default": 1, "min": 1, "max": 10000000000, "step": 1}),
                "steps": ("INT", {"default": 50, "min": 1, "max": 10000000000, "step": 1}),
                "sampler": (SamplerType._member_names_, {"default": "V_IPLMS"}),
                "sigma_min": ("FLOAT", {"default": 0.1, "min": 0.0, "max": 1280, "step": 0.01}),
                "sigma_max": ("FLOAT", {"default": 50, "min": 0.0, "max": 1280, "step": 0.01}),
                "rho": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 128.0, "step": 0.01}),
                "scheduler": (SchedulerType._member_names_, {"default": "V_CRASH"}),
                "noise_level": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.01}),
                "seed": ("INT", {"default": -1}),
                },
            "optional": {
                "input_tensor": ("AUDIO", {}),
                "input_audio_path": ("STRING", {"default": '', "forceInput": True}),
                },
            }

    RETURN_TYPES = ("LIST", "AUDIO", "INT")
    RETURN_NAMES = ("out_paths", "🎵audio", "sample_rate")
    FUNCTION = "do_sample"

    CATEGORY = "🎵Jags_Audio/AudioInference"

    def do_sample(self, audio_model, mode, batch_size, steps, sampler, sigma_min, sigma_max, rho, scheduler, input_audio_path='', input_tensor=None, noise_level=0.7, seed=-1):


        wrapper, inference = audio_model
        device_type_accelerator = get_torch_device()
        device_accelerator = torch.device(device_type_accelerator)
        device_offload = get_torch_device()

        crop = lambda audio: crop_audio(audio, wrapper.chunk_size, 0)

        if input_tensor is None:
            input_audio_path = None if input_audio_path == '' else input_audio_path
            load_input = lambda source: crop(load_audio(device_accelerator, source, wrapper.sample_rate)) if source is not None else None
            audio_source = load_input(input_audio_path)
        else:
            if len(input_tensor.shape) == 3:
            # remove first (batch) dimension
                input_tensor = input_tensor[0]
            if input_tensor.shape[0] != 2:
                channels, sample_length = input_tensor.shape
                input_tensor = input_tensor.view(1, sample_length).repeat(2, 1)
                input_tensor = input_tensor.to(get_torch_device())

            audio_source = crop(input_tensor)

        
        request_handler = RequestHandler(device_accelerator, device_offload, optimize_memory_use=False, use_autocast=True)
        
        seed = seed if(seed!=-1) else torch.randint(0, 4294967294, [1], device=device_type_accelerator).item()
        print(f"Using accelerator: {device_type_accelerator}, Seed: {seed}.")
        
        request = Request(
            request_type=mode,
            model_path=wrapper.path,
            model_type=ModelType.DD,
            model_chunk_size=wrapper.chunk_size,
            model_sample_rate=wrapper.sample_rate,
            model_wrapper=wrapper,
            model_inference=inference,
            
            seed=seed,
            batch_size=batch_size,
            
            audio_source=audio_source,
            audio_target=None,
            
            mask=None,
            
            noise_level=noise_level,
            interpolation_positions=None,
            resamples=None,
            keep_start=True,
                    
            steps=steps,
            
            sampler_type=SamplerType[sampler],
            sampler_args={'use_tqdm': True},
            
            scheduler_type=SchedulerType[scheduler],
            scheduler_args={
                'sigma_min': sigma_min,
                'sigma_max': sigma_max,
                'rho': rho,
            }
        )
        
        response = request_handler.process_request(request)#, lambda **kwargs: print(f"{kwargs['step'] / kwargs['x']}"))
        paths = save_audio(response.result, f"{comfy_dir}/temp", wrapper.sample_rate, f"{seed}_{random.randint(0, 100000)}")
        return (paths, response.result, wrapper.sample_rate)

class SaveAudio():
    def __init__(self):
        pass
    
    @classmethod
    def INPUT_TYPES(cls):
        """
        Input Types
        """
        return {
            "required": {
                "tensor": ("AUDIO", ),
                "output_path": ("STRING", {"default": f'{comfy_dir}/output/audio_samples'}),
                "sample_rate": ("INT", {"default": 44100, "min": 1, "max": 10000000000, "step": 1}),
                "id_string": ("STRING", {"default": 'ComfyUI'}),
                "tame": (['Enabled', 'Disabled'],)
                },
            "optional": {
                },
            }

    RETURN_TYPES = ("STRING", "AUDIO")
    RETURN_NAMES = ("path","🎵audio" )
    FUNCTION = "save_audio"
    OUTPUT_NODE = True

    CATEGORY = "🎵Jags_Audio"

    def save_audio(self, tensor, output_path, sample_rate, id_string, tame):
        return (save_audio(audio_out=(0.5 * tensor).clamp(-1,1) if(tame == 'Enabled') else tensor, output_path=output_path, sample_rate=sample_rate, id_str=id_string), )


class LoadAudio():
    def __init__(self):
        #self.input_audio = os.listdir(f'{comfy_dir}/../../audio_input')
        pass
    
    @classmethod
    def INPUT_TYPES(cls):
        """
        Input Types
        """
        return {
            "required": {
                ""
                "file_path": ("STRING", {}),
                },
            "optional": {
                },
            }
        
    RETURN_TYPES = ("STRING", "AUDIO", "INT" )
    RETURN_NAMES = ("path","🎵audio","samplerate")
    FUNCTION = "LoadAudio"
    OUTPUT_NODE = True

    CATEGORY = "🎵Jags_Audio"

    def LoadAudio(self, file_path):
        if file_path == '':
            waveform, samplerate = None, None
            return (file_path, waveform,samplerate,)

        #file_path = f'{comfy_dir}/custom_nodes/SampleDiffusion/audio_input/{file_path}'
        input_dir = folder_paths.get_input_directory()
        audio = os.path.join(input_dir, audio)
        file_path = folder_paths.get_annotated_filepath(audio)

        if file_path.endswith('.mp3'):
            if os.path.exists(file_path.replace('.mp3', '')+'.wav'):
                file_path = file_path.replace('.mp3', '')+'.wav'
            else:
                data, samplerate = sf.read(file_path)
                sf.write(file_path.replace('.mp3', '')+'.wav', data, samplerate)

            os.remove(file_path.replace('.wav', '.mp3'))

        waveform, samplerate = torchaudio.load(file_path)
        waveform = waveform.to(get_torch_device())
        waveform = waveform.unsqueeze(0)

        return (file_path, waveform, samplerate)

    """
    def LoadAudio(self, audio):
        input_dir = folder_paths.get_input_directory()
        audio = os.path.join(input_dir, audio)
        file = folder_paths.get_annotated_filepath(audio)
        if file is not None:
            audio = file
        if (file.lower().endswith('.mp3')):
            audio_file = AudioSegment.from_mp3(file)
        else:
            audio_file = AudioSegment.from_file(file, format="wav")
        
        audio_data = AudioData(audio_file)

        return (audio_data,)
        @classmethod
        def IS_CHANGED(self, audio, **kwargs):
            audio_path = folder_paths.get_annotated_filepath(audio)
        m = hashlib.sha256()
        with open(audio_path, 'rb') as f:
            m.update(f.read())
        return m.digest().hex()

        @classmethod
        def VALIDATE_INPUTS(self, audio, **kwargs):
            if not folder_paths.exists_annotated_filepath(audio):
                return "Invalid audio file: {}".format(audio)

        return True
    
        waveform, samplerate = torchaudio.load(audio)
        waveform = waveform.to(get_torch_device())
        waveform = waveform.unsqueeze(0)

        return (file_path, waveform, samplerate)
    """

#--------------------------------------------------------------------------------

class LoadAudioModelDD():    
    @classmethod
    def INPUT_TYPES(cls):
        """
        Input Types
        """
        global models_folder
        models = os.listdir(models_folder)
        models = [x for x in models if x.endswith('.ckpt')]
        return {
            "required": {
                ""
                "model": (models, {}),
                "chunk_size": ("INT", {"default": 65536, "min": 32768, "max": 10000000000, "step": 32768}),
                "sample_rate": ("INT", {"default": 44100, "min": 1, "max": 10000000000, "step": 1}),
                "optimize_memory_use": (['Enabled', 'Disabled'], {"default": 'Enabled'}),
                "autocast": (['Enabled', 'Disabled'], {"default": 'Enabled'}),
                },
            "optional": {
                },
            }

    RETURN_TYPES = ("DD_MODEL", )
    RETURN_NAMES = ("audio_model", )
    FUNCTION = "DoLoadAudioModelDD"
    OUTPUT_NODE = True

    CATEGORY = "🎵Jags_Audio/Audiotools"

    def DoLoadAudioModelDD(self, model, chunk_size, sample_rate, optimize_memory_use, autocast):
        global models_folder
        model = os.path.join(models_folder, model)
        device = get_torch_device()
        wrapper = DDModelWrapper()
        wrapper.load(model, device, optimize_memory_use, chunk_size, sample_rate)
        inference = DDInference(device, device, optimize_memory_use, autocast, wrapper)


        loaded_model = (wrapper, inference)

        return (loaded_model, )

class PreviewAudioFile():
    def __init__(self):
        pass
    
    @classmethod
    def INPUT_TYPES(cls):
        """
        Input Types
        """
        return {
            "required": {
                "tensor": ("AUDIO", ),
                "output_path": ("STRING", {"default": f'{comfy_dir}/output/audio_samples'}),
                "sample_rate": ("INT", {"default": 44100, "min": 1, "max": 10000000000, "step": 1}),
                "id_string": ("STRING", {"default": 'ComfyUI'}),
                "tame": (['Enabled', 'Disabled'],)
                },
            "optional": {
                },
            }

    RETURN_TYPES = ("AUDIO", "STRING", )
    RETURN_NAMES = ("🎵audio","paths", )
    FUNCTION = "PreviewAudioFile"
    OUTPUT_NODE = True

    CATEGORY = "🎵Jags_Audio"

    def PreviewAudioFile(self, paths):
        # fix slashes
        paths = [path.replace("\\", "/") for path in paths]
        # get filenames with extensions from paths

        filenames = [os.path.basename(path) for path in paths]
        return {"result": (filenames,), "ui": filenames}

class PreviewAudioTensor():
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "tensor": ("AUDIO",),
                "sample_rate": ("INT", {"default": 44100, "min": 1, "max": 10000000000, "step": 1, "forceInput": True}),
                "tame": (['Enabled', 'Disabled'],)
                },
            "optional": {
                },
            }

    RETURN_TYPES = ("AUDIO","LIST", )
    RETURN_NAMES = ("🎵audio","paths", )
    FUNCTION = "PreviewAudioTensor"
    OUTPUT_NODE = True

    CATEGORY = "🎵Jags_Audio"

    def PreviewAudioTensor(self, tensor, sample_rate, tame):
        # fix slashes
        paths = save_audio((0.5 * tensor).clamp(-1,1) if(tame == 'Enabled') else tensor, f"{comfy_dir}/temp", sample_rate, f"{random.randint(0, 10000000000)}")
        paths = [path.replace("\\", "/") for path in paths]
        # get filenames with extensions from paths
        paths = [os.path.basename(path) for path in paths]
        return {"result": (paths,), "ui": paths}

class MergeTensors():
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "tensor_1": ("AUDIO",),
                "tensor_2": ("AUDIO",),
                "tensor_1_volume": ("FLOAT", {"default": 1, "min": 0, "max": 1, "step": 0.01}),
                "tensor_2_volume": ("FLOAT", {"default": 1, "min": 0, "max": 1, "step": 0.01}),
                },
            "optional": {
                "sample_rate": ("INT", {"default": 44100, "min": 1, "max": 10000000000, "step": 1, "forceInput": True}),
                },
            }

    RETURN_TYPES = ("AUDIO", "INT")
    RETURN_NAMES = ("🎵audio", "sample_rate")
    FUNCTION = "do_merge"

    CATEGORY = "🎵Jags_Audio/Helpers"

    def do_merge(self, tensor_1, tensor_2, tensor_1_volume, tensor_2_volume, sample_rate):
        # Ensure both batches have the same size and number of channels
        assert tensor_1.size(0) == tensor_2.size(0) and tensor_1.size(1) == tensor_2.size(1), "Batches must have the same size and number of channels"

        # Pad or truncate the shorter waveforms in the batches to match the length of the longer ones
        max_length = max(tensor_1.size(2), tensor_2.size(2))
        tensor_1_padded = torch.zeros(tensor_1.size(0), tensor_1.size(1), max_length)
        tensor_2_padded = torch.zeros(tensor_2.size(0), tensor_2.size(1), max_length)

        tensor_1_padded[:, :, :tensor_1.size(2)] = tensor_1
        tensor_2_padded[:, :, :tensor_2.size(2)] = tensor_2

        # Mix the batches with specified volumes
        mixed_tensors = tensor_1_volume * tensor_1_padded + tensor_2_volume * tensor_2_padded

        return (mixed_tensors, sample_rate)


class StringListIndex:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "list": ("LIST", ),
                "index": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            },
        }

    RETURN_TYPES = ("STRING",)
    FUNCTION = "doStuff"
    CATEGORY = "🎵Jags_Audio/Helpers"

    def doStuff(self, list, index):
        return (list[index],)

class AudioIndex:
    def __init__(self) -> None:
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "path": ("STRING", {"default": ""}),
                "index": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            },
        }

    RETURN_TYPES = ("AUDIO", "INT", "STRING")
    RETURN_NAMES = ("🎵audio", "sample_rate", "filename")
    FUNCTION = "doStuff"
    CATEGORY = "🎵Jags_Audio/Helpers"

    def doStuff(self, path, index):
        if not os.path.exists(path):
            raise Exception("Path does not exist")
        audios = []
        for audio in os.listdir(path):
            if any(audio.endswith(ext) for ext in [".wav", ".flac"]):
                audios.append(audio)
        filename = audios[index]
        audio, sample_rate = torchaudio.load(os.path.join(path, filename))
        # make stereo if mono
        audio = audio.unsqueeze(0)
        audio.to(get_torch_device())
        return (audio, sample_rate, filename)
    
class IntegerNode:
    def __init__(self) -> None:
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "Value": ("FLOAT", {
                        "default": 1,
                        "min": -sys.maxsize,
                        "max": sys.maxsize,
                        "step": 1
                    },
                )
            },
        }

    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("SAMPLE_RATE",)
    CATEGORY = "🎵Jags_Audio/Helpers"
    FUNCTION = "sample_rate"

    def get_value(self, Value):
        return (int(Value),)

NODE_CLASS_MAPPINGS = {
    "GenerateAudioSample": AudioInference,
    "SaveAudioTensor": SaveAudio,
    "LoadAudioFile": LoadAudio,
    "PreviewAudioFile": PreviewAudioFile,
    "PreviewAudioTensor": PreviewAudioTensor,
    "GetStringByIndex": StringListIndex,
    "LoadAudioModel (DD)": LoadAudioModelDD,
    "MixAudioTensors": MergeTensors,
    "GetAudioFromFolderIndex": AudioIndex,
    "GetInteger": IntegerNode,
    
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "GenerateAudioSample": "Jags-AudioInference",
    "SaveAudioTensor": "Jags_SaveAudio",
    "LoadAudioFile": "Jags_LoadAudio",
    "PreviewAudioFile": "Jags_PreviewAudioFile",
    "PreviewAudioTensor": "Jags_PreviewAudioTensor",
    "GetStringByIndex": "Jags_StringListIndex",
    "LoadAudioModel (DD)": "Jags_LoadAudioModelDD",
    "MixAudioTensors": "Jags_MergeTensors",
    "GetAudioFromFolderIndex": "Jags_AudioIndex",
    "GetInteger": "Jags_SampleRate",
    
}

