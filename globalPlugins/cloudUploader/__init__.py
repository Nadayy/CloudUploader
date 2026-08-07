# Cloud Uploader for NVDA - uploads files to litterbox.catbox.moe (no account/API key needed)

import os
import sys
import threading
import time
import ctypes
import http.client
import mimetypes
import select
import socket
import uuid
import urllib.parse
import webbrowser
import json
import datetime
import wave
import math
import shutil
import subprocess
import tempfile
import array
import cmath

try:
	# Stdlib audioop was removed in Python 3.13 (NVDA 2026.1+). Without it,
	# every mix/resample/gain call below silently drops to the pure-Python
	# per-sample fallback loops, which is what was causing the multi-second
	# freeze after stopping a recording (a few minutes of 48kHz stereo audio
	# is tens of millions of samples processed one at a time in Python).
	# Prefer the real stdlib module when it's still present (NVDA < 2026.1),
	# otherwise fall back to the bundled audioop-lts build, which is the
	# same C implementation and keeps mix/resample at the original speed.
	import audioop
except ImportError:
	try:
		_libDir = os.path.join(os.path.dirname(__file__), "lib64")
		if _libDir not in sys.path:
			sys.path.insert(0, _libDir)
		import audioop
	except ImportError:
		audioop = None

import addonHandler
import api
import config
import core
import globalPluginHandler
import globalVars
import gui
import gui.guiHelper
import gui.settingsDialogs
import ui
import wx
from logHandler import log

addonHandler.initTranslation()

UPLOAD_HOST = "litterbox.catbox.moe"
UPLOAD_PATH = "/resources/internals/api.php"
HISTORY_MAX_ENTRIES_DEFAULT = 50

# Bump this whenever the wording below changes meaningfully, so returning
# users are shown the notice again instead of only brand-new installs.
TERMS_VERSION = "1"

TERMS_TEXT = _(
	"Cloud Uploader sends files to free, independently-operated third-party "
	"hosts (Litterbox, Catbox, Gofile, 0x0.st, Filebin, and Uguu), not a "
	"service run by this add-on. Each host has its own file size limits, "
	"content rules, and retention time, and violating a host's rules can get "
	"your uploads deleted and your IP address blocked from that host.\n\n"
	"In short: only upload content you have the right to share, that is "
	"legal, and that is reasonably sized. Do not rely on any of these hosts "
	"for anything sensitive, permanent, or high-volume.\n\n"
	"Full details for each host, including exact size limits and banned "
	"content, are in this add-on's documentation (NVDA menu > Help > "
	"Add-on Help, or the readme in the add-on's folder)."
)

confspec = {
	"defaultHost": "string(default='ask')",
	"autoCopyOnComplete": "boolean(default=false)",
	"maxHistoryEntries": "integer(default=50, min=1, max=200)",
	"showOnlyWorkingHosts": "boolean(default=false)",
	"ffmpegPath": "string(default='')",
	"micDeviceId": "integer(default=-1)",
	"micPreferMono": "boolean(default=false)",
	"systemDeviceId": "string(default='')",
	"silenceSensitivity": "string(default='medium')",
	"noiseReductionSensitivity": "string(default='medium')",
	"fileUploadOnly": "boolean(default=false)",
	"autoStartRecording": "boolean(default=false)",
	"recordingFormat": "string(default='mp3')",
	"audioQuality": "string(default='high')",
	"recordSourceMode": "string(default='mic')",
	"saveSeparateTracks": "boolean(default=false)",
	"micGainDb": "float(default=0.0, min=-20.0, max=20.0)",
	"systemGainDb": "float(default=0.0, min=-20.0, max=20.0)",
	"termsAcceptedVersion": "string(default='')",
}
config.conf.spec["cloudUploader"] = confspec

# (format key, file extension, spoken label)
RECORDING_FORMATS = [
	("mp3", ".mp3", _("MP3")),
	("wav", ".wav", _("WAV")),
	("flac", ".flac", _("FLAC")),
]

# (source mode key, spoken label)
RECORD_SOURCE_MODES = [
	("mic", _("Microphone only")),
	("computer", _("Computer audio only")),
	("both", _("Microphone and computer audio")),
]

# (quality key, spoken label, mp3 bitrate, ffmpeg sample rate, flac compression level)
AUDIO_QUALITY_LEVELS = [
	("low", _("Low"), "128k", 44100, "5"),
	("medium", _("Medium"), "192k", 44100, "6"),
	("high", _("High"), "320k", 48000, "8"),
]

# (spoken label, litterbox time code, seconds)
EXPIRY_OPTIONS = [
	(_("1 hour"), "1h", 3600),
	(_("12 hours"), "12h", 12 * 3600),
	(_("1 day"), "24h", 24 * 3600),
	(_("3 days"), "72h", 72 * 3600),
]

# (spoken label, gofile expiry code (unused, kept for interface consistency), seconds)
GOFILE_EXPIRY_OPTIONS = [
	(_("Default retention (about 10 days for anonymous uploads)"), None, 10 * 24 * 3600),
]

# (spoken label, catbox expiry code (unused - catbox.moe permanent uploads don't expire), seconds)
CATBOX_EXPIRY_OPTIONS = [
	(_("Permanent, kept indefinitely"), None, 3650 * 24 * 3600),
]
# (spoken label, 0x0.st expiry code (unused, kept for interface consistency), seconds)
# 0x0.st's actual policy: retention scales with file size, from 30 days
# (at the 512 MiB size limit) up to 1 year (for very small files).
ZEROXZERO_EXPIRY_OPTIONS = [
	(_("At least 30 days, up to 1 year for smaller files (larger files are kept for less time)"), None, 30 * 24 * 3600),
]

# (spoken label, filebin expiry code (unused, kept for interface consistency), seconds)
FILEBIN_EXPIRY_OPTIONS = [
	(_("Default retention (about 6 days)"), None, 6 * 24 * 3600),
]

# (spoken label, uguu expiry code (unused, kept for interface consistency), seconds)
UGUU_EXPIRY_OPTIONS = [
	(_("Automatic (temporary storage, about 48 hours)"), None, 48 * 3600),
]


def _getHistoryFilePath():
	folder = os.path.join(globalVars.appArgs.configPath, "cloudUploader")
	try:
		os.makedirs(folder, exist_ok=True)
	except Exception:
		pass
	return os.path.join(folder, "history.json")


def _getRecordingsFolder():
	folder = os.path.join(globalVars.appArgs.configPath, "cloudUploader", "recordings")
	try:
		os.makedirs(folder, exist_ok=True)
	except Exception:
		pass
	return folder


def _openRecordingsFolder():
	folder = _getRecordingsFolder()
	os.startfile(folder)


def _pruneOldRecordings(maxAgeSeconds=7 * 24 * 3600):
	"""Deletes leftover recorded clips older than maxAgeSeconds, so the
	recordings folder doesn't grow forever. Recordings the user has already
	uploaded (or decided not to keep) have no other reference once this
	plugin's session ends."""
	folder = _getRecordingsFolder()
	try:
		now = time.time()
		for name in os.listdir(folder):
			path = os.path.join(folder, name)
			try:
				if now - os.path.getmtime(path) > maxAgeSeconds:
					os.remove(path)
			except Exception:
				pass
	except Exception:
		pass


HIGH_QUALITY_CHANNELS = 2


def _findFfmpeg():
	configuredPath = ""
	try:
		configuredPath = config.conf["cloudUploader"]["ffmpegPath"]
	except Exception:
		pass
	if configuredPath and os.path.isfile(configuredPath):
		return configuredPath
	found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
	if found:
		return found
	# NVDA usually runs as a long-lived background process, so it can miss
	# PATH changes made after it started (e.g. installing ffmpeg via winget
	# or an installer after login). Check common install locations too.
	candidates = []
	programFiles = os.environ.get("ProgramFiles", "")
	programFilesX86 = os.environ.get("ProgramFiles(x86)", "")
	localAppData = os.environ.get("LOCALAPPDATA", "")
	chocolatey = os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey")
	if programFiles:
		candidates.append(os.path.join(programFiles, "ffmpeg", "bin", "ffmpeg.exe"))
	if programFilesX86:
		candidates.append(os.path.join(programFilesX86, "ffmpeg", "bin", "ffmpeg.exe"))
	if localAppData:
		candidates.append(os.path.join(localAppData, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
		candidates.append(os.path.join(localAppData, "Programs", "ffmpeg", "bin", "ffmpeg.exe"))
		wingetPackages = os.path.join(localAppData, "Microsoft", "WinGet", "Packages")
		if os.path.isdir(wingetPackages):
			try:
				for entry in os.listdir(wingetPackages):
					if "ffmpeg" in entry.lower():
						for root, _dirs, files in os.walk(os.path.join(wingetPackages, entry)):
							if "ffmpeg.exe" in files:
								candidates.append(os.path.join(root, "ffmpeg.exe"))
			except Exception:
				pass
	candidates.append(os.path.join(chocolatey, "bin", "ffmpeg.exe"))
	for path in candidates:
		if path and os.path.isfile(path):
			return path
	return None


def _runFfmpeg(args):
	creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
	result = subprocess.run(
		args,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		creationflags=creationflags,
	)
	return result


def _encodeToMp3(ffmpegPath, wavPath, mp3Path, bitrate, sampleRate):
	"""Encodes wavPath to an MP3 at the given bitrate/sample rate using
	ffmpeg. Raises with ffmpeg's own output on failure."""
	args = [
		ffmpegPath, "-y", "-i", wavPath,
		"-codec:a", "libmp3lame", "-b:a", bitrate,
		"-ar", str(sampleRate), "-ac", str(HIGH_QUALITY_CHANNELS),
		mp3Path,
	]
	result = _runFfmpeg(args)
	if result.returncode != 0 or not os.path.exists(mp3Path):
		raise Exception((result.stdout or b"").decode("utf-8", errors="replace")[-300:])


def _encodeToFlac(ffmpegPath, wavPath, flacPath, compressionLevel, sampleRate):
	"""Encodes wavPath to a lossless FLAC file using ffmpeg."""
	args = [
		ffmpegPath, "-y", "-i", wavPath,
		"-codec:a", "flac", "-compression_level", compressionLevel,
		"-ar", str(sampleRate), "-ac", str(HIGH_QUALITY_CHANNELS),
		flacPath,
	]
	result = _runFfmpeg(args)
	if result.returncode != 0 or not os.path.exists(flacPath):
		raise Exception((result.stdout or b"").decode("utf-8", errors="replace")[-300:])


def _readWavRaw(path):
	with wave.open(path, "rb") as wf:
		channels = wf.getnchannels()
		sampwidth = wf.getsampwidth()
		framerate = wf.getframerate()
		nframes = wf.getnframes()
		data = bytearray(wf.readframes(nframes))
	return data, channels, sampwidth, framerate


def _writeWavRaw(path, data, channels, sampwidth, framerate):
	with wave.open(path, "wb") as wf:
		wf.setnchannels(channels)
		wf.setsampwidth(sampwidth)
		wf.setframerate(framerate)
		wf.writeframes(bytes(data))


def _removeIfExists(path):
	if path and os.path.exists(path):
		try:
			os.remove(path)
		except Exception:
			pass


# Result codes RecordVoiceDialog.ShowModal() can return, in place of the
# old single "use this recording" wx.ID_OK: the dialog now offers three
# distinct post-recording actions instead of just handing back a path.
RESULT_UPLOAD_SAVE = 9001
RESULT_UPLOAD_NO_SAVE = 9002
RESULT_KEEP_NO_UPLOAD = 9003


_winmm = ctypes.windll.winmm

WAVE_FORMAT_PCM = 1
WAVE_MAPPER = 0xFFFFFFFF
CALLBACK_NULL = 0x00000000
WHDR_DONE = 0x00000001
MMSYSERR_NOERROR = 0


class WAVEFORMATEX(ctypes.Structure):
	_fields_ = [
		("wFormatTag", ctypes.c_ushort),
		("nChannels", ctypes.c_ushort),
		("nSamplesPerSec", ctypes.c_uint32),
		("nAvgBytesPerSec", ctypes.c_uint32),
		("nBlockAlign", ctypes.c_ushort),
		("wBitsPerSample", ctypes.c_ushort),
		("cbSize", ctypes.c_ushort),
	]


class WAVEHDR(ctypes.Structure):
	_fields_ = [
		("lpData", ctypes.c_void_p),
		("dwBufferLength", ctypes.c_uint32),
		("dwBytesRecorded", ctypes.c_uint32),
		("dwUser", ctypes.c_void_p),
		("dwFlags", ctypes.c_uint32),
		("dwLoops", ctypes.c_uint32),
		("lpNext", ctypes.c_void_p),
		("reserved", ctypes.c_void_p),
	]


_winmm.waveInOpen.argtypes = [
	ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.POINTER(WAVEFORMATEX),
	ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
]
_winmm.waveInOpen.restype = ctypes.c_uint32
_winmm.waveInPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint32]
_winmm.waveInPrepareHeader.restype = ctypes.c_uint32
_winmm.waveInAddBuffer.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint32]
_winmm.waveInAddBuffer.restype = ctypes.c_uint32
_winmm.waveInStart.argtypes = [ctypes.c_void_p]
_winmm.waveInStart.restype = ctypes.c_uint32
_winmm.waveInStop.argtypes = [ctypes.c_void_p]
_winmm.waveInStop.restype = ctypes.c_uint32
_winmm.waveInReset.argtypes = [ctypes.c_void_p]
_winmm.waveInReset.restype = ctypes.c_uint32
_winmm.waveInUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint32]
_winmm.waveInUnprepareHeader.restype = ctypes.c_uint32
_winmm.waveInClose.argtypes = [ctypes.c_void_p]
_winmm.waveInClose.restype = ctypes.c_uint32

_winmm.waveOutOpen.argtypes = [
	ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.POINTER(WAVEFORMATEX),
	ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
]
_winmm.waveOutOpen.restype = ctypes.c_uint32
_winmm.waveOutPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint32]
_winmm.waveOutPrepareHeader.restype = ctypes.c_uint32
_winmm.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint32]
_winmm.waveOutWrite.restype = ctypes.c_uint32
_winmm.waveOutUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint32]
_winmm.waveOutUnprepareHeader.restype = ctypes.c_uint32
_winmm.waveOutReset.argtypes = [ctypes.c_void_p]
_winmm.waveOutReset.restype = ctypes.c_uint32
_winmm.waveOutClose.argtypes = [ctypes.c_void_p]
_winmm.waveOutClose.restype = ctypes.c_uint32
_winmm.waveOutPause.argtypes = [ctypes.c_void_p]
_winmm.waveOutPause.restype = ctypes.c_uint32
_winmm.waveOutRestart.argtypes = [ctypes.c_void_p]
_winmm.waveOutRestart.restype = ctypes.c_uint32

MAXPNAMELEN = 32


class WAVEINCAPSW(ctypes.Structure):
	_fields_ = [
		("wMid", ctypes.c_ushort),
		("wPid", ctypes.c_ushort),
		("vDriverVersion", ctypes.c_uint32),
		("szPname", ctypes.c_wchar * MAXPNAMELEN),
		("dwFormats", ctypes.c_uint32),
		("wChannels", ctypes.c_ushort),
		("wReserved1", ctypes.c_ushort),
	]


_winmm.waveInGetNumDevs.restype = ctypes.c_uint32
_winmm.waveInGetDevCapsW.argtypes = [ctypes.c_uint32, ctypes.POINTER(WAVEINCAPSW), ctypes.c_uint32]
_winmm.waveInGetDevCapsW.restype = ctypes.c_uint32


def _listInputDevices():
	"""Returns a list of (deviceId, name) pairs for available recording
	devices, with deviceId -1 standing for 'system default'."""
	devices = [(-1, _("System default"))]
	try:
		count = _winmm.waveInGetNumDevs()
		for i in range(count):
			caps = WAVEINCAPSW()
			result = _winmm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
			if result == MMSYSERR_NOERROR:
				name = caps.szPname or (_("Device {index}").format(index=i))
				devices.append((i, name))
	except Exception as e:
		log.error("Cloud Uploader: could not enumerate recording devices: %s" % e)
	return devices


SILENCE_PRESETS = {
	"light": dict(thresholdRatio=0.08, minSilenceMs=900, keepPaddingMs=350),
	"medium": dict(thresholdRatio=0.15, minSilenceMs=600, keepPaddingMs=250),
	"aggressive": dict(thresholdRatio=0.30, minSilenceMs=350, keepPaddingMs=120),
}


class _WaveInRecorder(object):
	"""Records raw PCM straight from the Windows waveIn API instead of going
	through MCI's "set format" command, which many drivers silently ignore.
	Asking waveInOpen directly for 48kHz/16-bit/stereo makes Windows' audio
	engine do the necessary resampling/channel conversion itself, so the
	requested quality is actually enforced rather than just requested.
	Only falls back to a lower format if the device genuinely rejects
	everything better."""

	CANDIDATE_FORMATS_STEREO = [
		(48000, 2, 16),
		(44100, 2, 16),
		(48000, 1, 16),
		(44100, 1, 16),
	]
	CANDIDATE_FORMATS_MONO = [
		(48000, 1, 16),
		(44100, 1, 16),
	]
	BUFFER_SECONDS = 0.5
	NUM_BUFFERS = 6

	def __init__(self, deviceId=-1, preferMono=False):
		self._handle = None
		self._headers = []
		self._buffers = []
		self._chunks = []
		self._deviceId = deviceId if deviceId is not None else -1
		self._candidateFormats = self.CANDIDATE_FORMATS_MONO if preferMono else self.CANDIDATE_FORMATS_STEREO
		self.samplerate = None
		self.channels = None
		self.bitspersample = 16
		self.usedFallbackDevice = False
		self._capturing = False

	def open(self):
		"""Opens the device and queues buffers without starting capture yet.
		Call startCapture() when both recorders are ready so dual-source
		recordings begin as close together as possible."""
		lastError = None
		deviceIdValue = ctypes.c_uint32(WAVE_MAPPER if self._deviceId < 0 else self._deviceId)
		for rate, channels, bits in self._candidateFormats:
			fmt = WAVEFORMATEX()
			fmt.wFormatTag = WAVE_FORMAT_PCM
			fmt.nChannels = channels
			fmt.nSamplesPerSec = rate
			fmt.wBitsPerSample = bits
			fmt.nBlockAlign = channels * (bits // 8)
			fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign
			fmt.cbSize = 0
			handle = ctypes.c_void_p()
			result = _winmm.waveInOpen(
				ctypes.byref(handle), deviceIdValue, ctypes.byref(fmt),
				None, None, CALLBACK_NULL,
			)
			if result == MMSYSERR_NOERROR:
				self._handle = handle
				self.samplerate = rate
				self.channels = channels
				self.bitspersample = bits
				break
			lastError = result
		if self._handle is None and deviceIdValue.value != WAVE_MAPPER:
			deviceIdValue = ctypes.c_uint32(WAVE_MAPPER)
			for rate, channels, bits in self._candidateFormats:
				fmt = WAVEFORMATEX()
				fmt.wFormatTag = WAVE_FORMAT_PCM
				fmt.nChannels = channels
				fmt.nSamplesPerSec = rate
				fmt.wBitsPerSample = bits
				fmt.nBlockAlign = channels * (bits // 8)
				fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign
				fmt.cbSize = 0
				handle = ctypes.c_void_p()
				result = _winmm.waveInOpen(
					ctypes.byref(handle), deviceIdValue, ctypes.byref(fmt),
					None, None, CALLBACK_NULL,
				)
				if result == MMSYSERR_NOERROR:
					self._handle = handle
					self.samplerate = rate
					self.channels = channels
					self.bitspersample = bits
					self.usedFallbackDevice = True
					break
				lastError = result
		if self._handle is None:
			raise Exception(
				_("The recording device did not accept any supported audio format (error {code})").format(
					code=lastError
				)
			)

		blockAlign = self.channels * (self.bitspersample // 8)
		frameCount = int(self.samplerate * self.BUFFER_SECONDS)
		bufferBytes = max(blockAlign, frameCount * blockAlign)
		for _i in range(self.NUM_BUFFERS):
			buf = ctypes.create_string_buffer(bufferBytes)
			header = WAVEHDR()
			header.lpData = ctypes.cast(buf, ctypes.c_void_p)
			header.dwBufferLength = bufferBytes
			header.dwFlags = 0
			result = _winmm.waveInPrepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
			if result != MMSYSERR_NOERROR:
				raise Exception(_("Could not prepare the recording buffer (error {code})").format(code=result))
			result = _winmm.waveInAddBuffer(self._handle, ctypes.byref(header), ctypes.sizeof(header))
			if result != MMSYSERR_NOERROR:
				raise Exception(_("Could not queue the recording buffer (error {code})").format(code=result))
			self._buffers.append(buf)
			self._headers.append(header)

	def startCapture(self):
		if self._handle is None or self._capturing:
			return
		result = _winmm.waveInStart(self._handle)
		if result != MMSYSERR_NOERROR:
			raise Exception(_("Could not start recording (error {code})").format(code=result))
		self._capturing = True

	def start(self):
		self.open()
		self.startCapture()

	def poll(self):
		"""Collects any buffers the driver has finished filling and requeues
		them. Call this periodically (e.g. from a wx.Timer) while
		recording, or data will be dropped once all buffers fill up."""
		if self._handle is None:
			return
		for header in self._headers:
			if header.dwFlags & WHDR_DONE:
				length = header.dwBytesRecorded
				if length:
					self._chunks.append(ctypes.string_at(header.lpData, length))
				header.dwFlags &= ~WHDR_DONE
				header.dwBytesRecorded = 0
				_winmm.waveInAddBuffer(self._handle, ctypes.byref(header), ctypes.sizeof(header))

	def stop(self):
		if self._handle is None:
			return b""
		_winmm.waveInStop(self._handle)
		self.poll()
		for header in self._headers:
			_winmm.waveInUnprepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
		_winmm.waveInClose(self._handle)
		self._handle = None
		return b"".join(self._chunks)

	def abort(self):
		if self._handle is None:
			return
		try:
			_winmm.waveInReset(self._handle)
			for header in self._headers:
				_winmm.waveInUnprepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
			_winmm.waveInClose(self._handle)
		except Exception:
			pass
		self._handle = None


try:
	import comtypes
	from comtypes import GUID, COMMETHOD, HRESULT, POINTER as _P
except ImportError:
	comtypes = None


def _asInt(value):
	"""Normalizes a COM out-parameter that comtypes may hand back as a
	plain int or as a ctypes scalar object, depending on version."""
	if hasattr(value, "value"):
		return value.value
	return int(value)


def _asVoidPtr(value):
	"""Normalizes a COM out-parameter pointer that comtypes may hand back
	as a plain int, a ctypes.c_void_p, or a ctypes pointer object - all
	of which ctypes.cast()/string_at() accept, but not interchangeably
	in every comtypes version."""
	if value is None:
		return None
	if isinstance(value, int):
		return value
	if hasattr(value, "value"):
		v = value.value
		return value if v is None else v
	return value


def _isNullPtr(value):
	"""True if a COM out-pointer (interface, void* or plain address)
	normalizes to NULL. Some drivers hand back NULL with a "success"
	HRESULT, and calling through a NULL interface's vtable is a native
	access violation that no try/except can catch - so it must be
	checked before use."""
	if value is None:
		return True
	v = _asVoidPtr(value)
	if v is None:
		return True
	if isinstance(v, int):
		return v == 0
	try:
		return not bool(v)
	except Exception:
		return False


class _LoopbackRecorder(object):
	"""Captures whatever is playing through the system's default output
	device ("computer audio") via WASAPI loopback, separately from
	_WaveInRecorder (mic input only). Always normalized to 16-bit
	stereo so it mixes predictably with a (possibly mono) mic track."""

	OUT_CHANNELS = 2
	OUT_SAMPWIDTH = 2

	CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}") if comtypes else None
	IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}") if comtypes else None
	IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}") if comtypes else None
	IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}") if comtypes else None
	IID_IMMDeviceCollection = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}") if comtypes else None
	AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
	AUDCLNT_SHAREMODE_SHARED = 0
	AUDCLNT_BUFFERFLAGS_SILENT = 0x2

	def __init__(self, deviceId=None):
		self._client = None
		self._captureClient = None
		self._device = None
		self._enumerator = None
		self._chunks = []
		self._deviceId = deviceId or None
		self.usedFallbackDevice = False
		self.samplerate = None
		self.channels = self.OUT_CHANNELS
		self.bitspersample = self.OUT_SAMPWIDTH * 8
		self._srcChannels = None
		self._srcSampwidth = None
		self._srcIsFloat = False

	def _buildInterfaces(self):
		"""Defines the small slice of IMMDeviceEnumerator/IMMDeviceCollection/
		IMMDevice/IAudioClient/IAudioCaptureClient needed here as comtypes
		interfaces. Built lazily (once) so importing this module never
		touches COM unless a recording using computer audio is actually
		attempted."""
		if hasattr(_LoopbackRecorder, "_IMMDeviceEnumerator"):
			return

		class IAudioCaptureClient(comtypes.IUnknown):
			_iid_ = _LoopbackRecorder.IID_IAudioCaptureClient
			_methods_ = [
				COMMETHOD([], HRESULT, "GetBuffer",
					(["out"], _P(_P(ctypes.c_byte)), "ppData"),
					(["out"], _P(ctypes.c_uint32), "pNumFramesToRead"),
					(["out"], _P(ctypes.c_uint32), "pdwFlags"),
					(["out"], _P(ctypes.c_uint64), "pu64DevicePosition"),
					(["out"], _P(ctypes.c_uint64), "pu64QPCPosition"),
				),
				COMMETHOD([], HRESULT, "ReleaseBuffer",
					(["in"], ctypes.c_uint32, "NumFramesRead"),
				),
				COMMETHOD([], HRESULT, "GetNextPacketSize",
					(["out"], _P(ctypes.c_uint32), "pNumFramesInNextPacket"),
				),
			]

		class IAudioClient(comtypes.IUnknown):
			_iid_ = _LoopbackRecorder.IID_IAudioClient
			_methods_ = [
				COMMETHOD([], HRESULT, "Initialize",
					(["in"], ctypes.c_int, "ShareMode"),
					(["in"], ctypes.c_uint32, "StreamFlags"),
					(["in"], ctypes.c_int64, "hnsBufferDuration"),
					(["in"], ctypes.c_int64, "hnsPeriodicity"),
					(["in"], ctypes.c_void_p, "pFormat"),
					(["in"], ctypes.c_void_p, "AudioSessionGuid"),
				),
				COMMETHOD([], HRESULT, "GetBufferSize", (["out"], _P(ctypes.c_uint32), "pNumBufferFrames")),
				COMMETHOD([], HRESULT, "_GetStreamLatency_unused", (["out"], _P(ctypes.c_int64), "p")),
				COMMETHOD([], HRESULT, "_GetCurrentPadding_unused", (["out"], _P(ctypes.c_uint32), "p")),
				COMMETHOD([], HRESULT, "_IsFormatSupported_unused",
					(["in"], ctypes.c_int, "ShareMode"),
					(["in"], ctypes.c_void_p, "pFormat"),
					(["out"], _P(ctypes.c_void_p), "ppClosestMatch"),
				),
				COMMETHOD([], HRESULT, "GetMixFormat", (["out"], _P(ctypes.c_void_p), "ppDeviceFormat")),
				COMMETHOD([], HRESULT, "_GetDevicePeriod_unused",
					(["out"], _P(ctypes.c_int64), "a"), (["out"], _P(ctypes.c_int64), "b")),
				COMMETHOD([], HRESULT, "Start"),
				COMMETHOD([], HRESULT, "Stop"),
				COMMETHOD([], HRESULT, "Reset"),
				COMMETHOD([], HRESULT, "_SetEventHandle_unused", (["in"], ctypes.c_void_p, "e")),
				COMMETHOD([], HRESULT, "GetService",
					(["in"], _P(GUID), "riid"),
					(["out"], _P(ctypes.c_void_p), "ppv"),
				),
			]

		class IMMDevice(comtypes.IUnknown):
			_iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
			_methods_ = [
				COMMETHOD([], HRESULT, "Activate",
					(["in"], _P(GUID), "iid"),
					(["in"], ctypes.c_uint32, "dwClsCtx"),
					(["in"], ctypes.c_void_p, "pActivationParams"),
					(["out"], _P(ctypes.c_void_p), "ppInterface"),
				),
				# Vtable slot kept for layout; friendly-name lookup via
				# IPropertyStore was removed (it caused NVDA crashes).
				COMMETHOD([], HRESULT, "_OpenPropertyStore_unused",
					(["in"], ctypes.c_uint32, "stgmAccess"),
					(["out"], _P(ctypes.c_void_p), "ppProperties"),
				),
				COMMETHOD([], HRESULT, "GetId",
					(["out"], _P(ctypes.c_wchar_p), "ppstrId"),
				),
				COMMETHOD([], HRESULT, "_GetState_unused",
					(["out"], _P(ctypes.c_uint32), "pdwState"),
				),
			]

		class IMMDeviceCollection(comtypes.IUnknown):
			_iid_ = _LoopbackRecorder.IID_IMMDeviceCollection
			_methods_ = [
				COMMETHOD([], HRESULT, "GetCount", (["out"], _P(ctypes.c_uint32), "pcDevices")),
				COMMETHOD([], HRESULT, "Item",
					(["in"], ctypes.c_uint32, "nDevice"),
					(["out"], _P(ctypes.POINTER(IMMDevice)), "ppDevice"),
				),
			]

		class IMMDeviceEnumerator(comtypes.IUnknown):
			_iid_ = _LoopbackRecorder.IID_IMMDeviceEnumerator
			_methods_ = [
				COMMETHOD([], HRESULT, "EnumAudioEndpoints",
					(["in"], ctypes.c_int, "dataFlow"),
					(["in"], ctypes.c_uint32, "dwStateMask"),
					(["out"], _P(ctypes.POINTER(IMMDeviceCollection)), "ppDevices"),
				),
				COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
					(["in"], ctypes.c_int, "dataFlow"),
					(["in"], ctypes.c_int, "role"),
					(["out"], _P(ctypes.POINTER(IMMDevice)), "ppEndpoint"),
				),
				COMMETHOD([], HRESULT, "GetDevice",
					(["in"], ctypes.c_wchar_p, "pwstrId"),
					(["out"], _P(ctypes.POINTER(IMMDevice)), "ppDevice"),
				),
			]

		_LoopbackRecorder._IMMDevice = IMMDevice
		_LoopbackRecorder._IMMDeviceCollection = IMMDeviceCollection
		_LoopbackRecorder._IMMDeviceEnumerator = IMMDeviceEnumerator
		_LoopbackRecorder._IAudioClient = IAudioClient
		_LoopbackRecorder._IAudioCaptureClient = IAudioCaptureClient

	def open(self):
		"""Prepare the loopback client without starting capture yet."""
		if comtypes is None:
			raise Exception(_("Computer audio recording is unavailable on this system"))
		self._buildInterfaces()
		try:
			comtypes.CoInitialize()
		except Exception:
			pass
		enumerator = comtypes.CoCreateInstance(
			self.CLSID_MMDeviceEnumerator, interface=self._IMMDeviceEnumerator,
			clsctx=comtypes.CLSCTX_ALL,
		)
		if _isNullPtr(enumerator):
			raise Exception("Could not create the audio device enumerator")
		eRender, eConsole = 0, 0
		device = None
		if self._deviceId:
			try:
				device = enumerator.GetDevice(self._deviceId)
				if _isNullPtr(device):
					device = None
			except Exception as e:
				log.error("Cloud Uploader: chosen playback device unavailable, falling back to default: %s" % e)
		if device is None:
			device = enumerator.GetDefaultAudioEndpoint(eRender, eConsole)
			self.usedFallbackDevice = bool(self._deviceId)
		if _isNullPtr(device):
			raise Exception("No playback device is available for computer audio recording")
		CLSCTX_ALL = comtypes.CLSCTX_ALL
		clientPtr = device.Activate(ctypes.byref(self.IID_IAudioClient), CLSCTX_ALL, None)
		if _isNullPtr(clientPtr):
			raise Exception("The playback device did not return an audio client")
		client = ctypes.cast(_asVoidPtr(clientPtr), ctypes.POINTER(self._IAudioClient))

		fmtPtr = client.GetMixFormat()
		if _isNullPtr(fmtPtr):
			raise Exception("The playback device did not return a mix format")
		fmt = ctypes.cast(_asVoidPtr(fmtPtr), ctypes.POINTER(_WAVEFORMATEX_FULL)).contents
		self._srcChannels = fmt.nChannels
		self.samplerate = fmt.nSamplesPerSec
		self._srcSampwidth = fmt.wBitsPerSample // 8
		if fmt.wFormatTag == 3:
			self._srcIsFloat = True
		elif fmt.wFormatTag == 0xFFFE:
			ext = ctypes.cast(_asVoidPtr(fmtPtr), ctypes.POINTER(_WAVEFORMATEXTENSIBLE_FULL)).contents
			self._srcIsFloat = ext.subFormatTag == 3
		else:
			self._srcIsFloat = False

		hnsBuffer = 3 * 1000 * 1000  # ~300ms
		try:
			client.Initialize(
				self.AUDCLNT_SHAREMODE_SHARED,
				self.AUDCLNT_STREAMFLAGS_LOOPBACK,
				hnsBuffer, 0, fmtPtr, None,
			)
		finally:
			try:
				ctypes.windll.ole32.CoTaskMemFree(_asVoidPtr(fmtPtr))
			except Exception:
				pass
		capturePtr = client.GetService(ctypes.byref(self.IID_IAudioCaptureClient))
		if _isNullPtr(capturePtr):
			raise Exception("The audio client did not return a capture client")
		captureClient = ctypes.cast(_asVoidPtr(capturePtr), ctypes.POINTER(self._IAudioCaptureClient))
		self._device = device
		self._enumerator = enumerator
		self._client = client
		self._captureClient = captureClient
		self._capturing = False

	def startCapture(self):
		if self._client is None or getattr(self, "_capturing", False):
			return
		self._client.Start()
		self._capturing = True

	def start(self):
		self.open()
		self.startCapture()

	def poll(self):
		if self._captureClient is None:
			return
		while True:
			try:
				packetFrames = _asInt(self._captureClient.GetNextPacketSize())
				if packetFrames == 0:
					return
				dataPtr, numFrames, flags, devPos, qpcPos = self._captureClient.GetBuffer()
				numFrames = _asInt(numFrames)
				flags = _asInt(flags)
				frameBytes = self._srcChannels * self._srcSampwidth
				byteLen = numFrames * frameBytes
				if numFrames and not (flags & self.AUDCLNT_BUFFERFLAGS_SILENT) and not _isNullPtr(dataPtr):
					raw = ctypes.string_at(_asVoidPtr(dataPtr), byteLen)
					self._chunks.append(self._convertChunk(raw))
				elif numFrames:
					# Silent packet: emit actual silence of the right (converted) length.
					outFrameBytes = self.OUT_CHANNELS * self.OUT_SAMPWIDTH
					self._chunks.append(b"\x00" * (numFrames * outFrameBytes))
				self._captureClient.ReleaseBuffer(numFrames)
			except Exception as e:
				# A single glitched packet shouldn't take down the whole
				# recording - log it and just stop draining for this tick;
				# the next poll (or the final one in stop()) picks back up.
				log.error("Cloud Uploader: computer audio capture glitch: %s" % e)
				return

	def _convertChunk(self, raw):
		"""Converts one captured buffer to 16-bit stereo PCM, from whatever
		the device's shared-mode mix format actually was (commonly 32-bit
		float, possibly more than 2 channels, but some virtual audio
		devices use plain integer PCM instead)."""
		srcCh = self._srcChannels
		if self._srcIsFloat:
			floats = array.array("f")
			floats.frombytes(raw)
			frameCount = len(floats) // srcCh
			out = array.array("h", bytes(frameCount * self.OUT_CHANNELS * self.OUT_SAMPWIDTH))
			for i in range(frameCount):
				base = i * srcCh
				if srcCh >= 2:
					left, right = floats[base], floats[base + 1]
				else:
					left = right = floats[base]
				out[i * 2] = _clampFloatToInt16(left)
				out[i * 2 + 1] = _clampFloatToInt16(right)
			return out.tobytes()
		elif self._srcSampwidth == 2:
			ints = array.array("h")
			ints.frombytes(raw)
			frameCount = len(ints) // srcCh
			out = array.array("h", bytes(frameCount * self.OUT_CHANNELS * self.OUT_SAMPWIDTH))
			for i in range(frameCount):
				base = i * srcCh
				if srcCh >= 2:
					left, right = ints[base], ints[base + 1]
				else:
					left = right = ints[base]
				out[i * 2] = left
				out[i * 2 + 1] = right
			return out.tobytes()
		elif self._srcSampwidth == 4:
			# 32-bit integer PCM - common on virtual audio cables (VB-Cable,
			# VoiceMeeter, and similar) that don't use the float mix format
			# most physical sound cards default to. Taking the high 16 bits
			# of each sample scales it the same way a native 16-bit
			# recording would be.
			ints = array.array("i")
			ints.frombytes(raw)
			frameCount = len(ints) // srcCh
			out = array.array("h", bytes(frameCount * self.OUT_CHANNELS * self.OUT_SAMPWIDTH))
			for i in range(frameCount):
				base = i * srcCh
				if srcCh >= 2:
					left, right = ints[base], ints[base + 1]
				else:
					left = right = ints[base]
				out[i * 2] = max(-32768, min(32767, left >> 16))
				out[i * 2 + 1] = max(-32768, min(32767, right >> 16))
			return out.tobytes()
		else:
			# Genuinely unusual sample format (e.g. 24-bit packed int):
			# skip rather than risk misinterpreting raw bytes as audio.
			return b""

	def stop(self):
		if self._client is None:
			return b""
		try:
			self._client.Stop()
		except Exception:
			pass
		self.poll()
		self._teardown()
		return b"".join(self._chunks)

	def abort(self):
		if self._client is None:
			return
		try:
			self._client.Stop()
		except Exception:
			pass
		self._teardown()

	def _teardown(self):
		self._captureClient = None
		self._client = None
		self._device = None
		self._enumerator = None
		# Deliberately not calling comtypes.CoUninitialize() here: NVDA's
		# main thread already has its own COM lifetime, initialized before
		# this code ever runs and expected to stay that way for as long as
		# NVDA is alive. Un-initializing it out from under NVDA - possible
		# if our earlier CoInitialize() call was a no-op because the
		# thread was already initialized in a different apartment mode -
		# is exactly the kind of thing that causes recording to fail
		# intermittently rather than consistently.


class _WAVEFORMATEX_FULL(ctypes.Structure):
	_fields_ = [
		("wFormatTag", ctypes.c_ushort),
		("nChannels", ctypes.c_ushort),
		("nSamplesPerSec", ctypes.c_uint32),
		("nAvgBytesPerSec", ctypes.c_uint32),
		("nBlockAlign", ctypes.c_ushort),
		("wBitsPerSample", ctypes.c_ushort),
		("cbSize", ctypes.c_ushort),
	]


class _WAVEFORMATEXTENSIBLE_FULL(ctypes.Structure):
	"""WAVEFORMATEXTENSIBLE, with the SubFormat GUID reduced to just its
	first 4 bytes (the part that actually varies between the PCM and
	IEEE-float subtypes we care about) rather than needing a full GUID
	type - keeps this struct definable with no comtypes dependency."""
	_fields_ = [
		("wFormatTag", ctypes.c_ushort),
		("nChannels", ctypes.c_ushort),
		("nSamplesPerSec", ctypes.c_uint32),
		("nAvgBytesPerSec", ctypes.c_uint32),
		("nBlockAlign", ctypes.c_ushort),
		("wBitsPerSample", ctypes.c_ushort),
		("cbSize", ctypes.c_ushort),
		("wValidBitsPerSample", ctypes.c_ushort),
		("dwChannelMask", ctypes.c_uint32),
		("subFormatTag", ctypes.c_uint32),
		("_subFormatRest", ctypes.c_ubyte * 12),
	]


def _listOutputDevices():
	"""Returns (deviceId, name) pairs for playback devices computer audio
	can be captured from - deviceId is None for the system default, or a
	stable ID comtypes/WASAPI can look up later. Always includes the
	default entry so the control using this list is never empty.

	Friendly names via IPropertyStore were removed: that COM wrapper's
	pointers access-violated on release during Python GC. Labels are a
	short stable form derived from the device ID instead."""
	devices = [(None, _("System default"))]
	if comtypes is None:
		return devices
	try:
		recorder = _LoopbackRecorder()
		recorder._buildInterfaces()
		try:
			comtypes.CoInitialize()
		except Exception:
			pass
		enumerator = comtypes.CoCreateInstance(
			_LoopbackRecorder.CLSID_MMDeviceEnumerator,
			interface=_LoopbackRecorder._IMMDeviceEnumerator,
			clsctx=comtypes.CLSCTX_ALL,
		)
		if _isNullPtr(enumerator):
			return devices
		eRender = 0
		DEVICE_STATE_ACTIVE = 0x1
		collection = enumerator.EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE)
		if _isNullPtr(collection):
			return devices
		count = _asInt(collection.GetCount())
		for i in range(count):
			try:
				device = collection.Item(i)
				if _isNullPtr(device):
					continue
				# GetId allocates a string with CoTaskMemAlloc. comtypes may
				# hand us a Python str (already freed) or a raw c_wchar_p;
				# free only when we still have a native pointer.
				rawId = device.GetId()
				devId = None
				needFree = False
				try:
					if isinstance(rawId, str):
						devId = rawId
					elif hasattr(rawId, "value"):
						devId = rawId.value
						needFree = True
					elif rawId:
						devId = ctypes.wstring_at(rawId) if not isinstance(rawId, str) else rawId
						needFree = True
				finally:
					if needFree and rawId:
						try:
							ptr = rawId if isinstance(rawId, int) else getattr(rawId, "value", rawId)
							if ptr:
								ctypes.windll.ole32.CoTaskMemFree(ptr)
						except Exception:
							pass
				if not devId:
					continue
				# Short label: last GUID segment of the endpoint ID, if present.
				# Full IDs look like {0.0.0.00000000}.{xxxxxxxx-...}; the
				# trailing GUID is unique enough for the choice list.
				short = devId
				if "}" in devId:
					parts = devId.split("}")
					if len(parts) >= 2 and parts[-2]:
						short = parts[-2].split("{")[-1] or devId
				name = _("Playback device {n} ({id})").format(n=i + 1, id=short[:13])
				devices.append((devId, name))
			except Exception as e:
				log.error("Cloud Uploader: could not read a playback device's details: %s" % e)
	except Exception as e:
		log.error("Cloud Uploader: could not enumerate playback devices: %s" % e)
	return devices


def _clampFloatToInt16(v):
	iv = int(v * 32768.0)
	if iv > 32767:
		return 32767
	if iv < -32768:
		return -32768
	return iv


def _resamplePcm16(data, channels, fromRate, toRate):
	"""Lines up two independently captured tracks (microphone and computer
	audio) to a common rate before mixing them into one file. Recordings
	kept as separate tracks are never resampled, so their original
	quality is untouched. Uses audioop's C-level rate converter when
	available - orders of magnitude faster than resampling in a Python
	loop - falling back to simple linear interpolation only if audioop
	is missing."""
	if fromRate == toRate or not data:
		return bytearray(data)
	if audioop is not None:
		trimmed = bytes(data[:len(data) - (len(data) % (2 * channels))])
		converted, _state = audioop.ratecv(trimmed, 2, channels, fromRate, toRate, None)
		return bytearray(converted)
	samples = array.array("h")
	samples.frombytes(bytes(data[:len(data) - (len(data) % (2 * channels))]))
	frameCount = len(samples) // channels
	if frameCount == 0:
		return bytearray()
	ratio = fromRate / float(toRate)
	outFrameCount = max(1, int(frameCount / ratio))
	out = array.array("h", bytes(outFrameCount * channels * 2))
	for i in range(outFrameCount):
		srcPos = i * ratio
		srcIdx = int(srcPos)
		frac = srcPos - srcIdx
		nextIdx = min(srcIdx + 1, frameCount - 1)
		for ch in range(channels):
			a = samples[srcIdx * channels + ch]
			b = samples[nextIdx * channels + ch]
			out[i * channels + ch] = int(a + (b - a) * frac)
	return bytearray(out.tobytes())


def _toStereo16(data, channels):
	"""Upmixes mono 16-bit PCM to stereo by duplicating the single channel;
	returns stereo data unchanged. Used to bring a mono microphone track
	up to stereo when mixing it with computer audio, which is always kept
	stereo. Uses audioop.tostereo (C-level) when available."""
	if channels == 2:
		return bytearray(data)
	if channels != 1:
		return bytearray(data)
	trimmed = bytes(data[:len(data) - (len(data) % 2)])
	if audioop is not None:
		return bytearray(audioop.tostereo(trimmed, 2, 1, 1))
	samples = array.array("h")
	samples.frombytes(trimmed)
	out = array.array("h", bytes(len(samples) * 2 * 2))
	for i, v in enumerate(samples):
		out[i * 2] = v
		out[i * 2 + 1] = v
	return bytearray(out.tobytes())


def _processCapturedAudio(
	micRaw, micChannels, micSampwidth, micRate,
	sysRaw, sysRate, sourceMode, separate,
	outPath, micPath, sysPath, startClockOffset,
):
	"""Aligns dual-source tracks, mixes at 0 dB, writes WAV file(s), and
	returns a dict of buffers/params for the record dialog (or background
	recording hand-off). Raises on failure."""
	micData = bytearray(micRaw) if micRaw is not None else None
	sysData = bytearray(sysRaw) if sysRaw is not None else None
	if micData is not None and sysData is not None:
		micFrame = micChannels * micSampwidth
		sysFrame = 4  # stereo 16-bit
		# Use the gap actually measured between the two startCapture()
		# calls, not one inferred from recorded length.
		diff = startClockOffset
		if diff > 0.0005:
			drop = int(diff * micRate) * micFrame
			if 0 < drop < len(micData):
				micData = micData[drop:]
		elif diff < -0.0005:
			drop = int((-diff) * sysRate) * sysFrame
			if 0 < drop < len(sysData):
				sysData = sysData[drop:]
	mixBaseMic, mixBaseSys, current, channels, mixRate = _buildMix(
		micData, micChannels, micRate, sysData, sysRate, 0.0, 0.0
	)
	_removeIfExists(outPath)
	_writeWavRaw(outPath, current, channels, 2, mixRate)
	if sourceMode == "both" and separate:
		if micData is not None:
			_removeIfExists(micPath)
			_writeWavRaw(micPath, micData, micChannels, micSampwidth, micRate)
		if sysData is not None:
			_removeIfExists(sysPath)
			_writeWavRaw(sysPath, sysData, 2, 2, sysRate)
	return {
		"micData": micData,
		"micChannels": micChannels,
		"micSampwidth": micSampwidth,
		"micRate": micRate,
		"sysData": sysData,
		"sysRate": sysRate,
		"mixBaseMic": mixBaseMic,
		"mixBaseSys": mixBaseSys,
		"current": current,
		"channels": channels,
		"mixRate": mixRate,
		"sourceMode": sourceMode,
		"separate": separate,
		"outPath": outPath,
		"micPath": micPath,
		"sysPath": sysPath,
	}


def _buildMix(micData, micChannels, micRate, sysData, sysRate, micGainDb, sysGainDb, micBase=None, sysBase=None):
	"""Builds (or reuses) pre-resampled stereo bases and returns
	(micBase, sysBase, mixedCurrent, channels, mixRate).
	When both tracks exist, bases are stereo PCM at mixRate without gain;
	volume changes can then skip resampling."""
	if micData is not None and sysData is not None:
		mixRate = sysRate or micRate
		if micBase is None:
			micBase = _toStereo16(micData, micChannels)
			if micRate != mixRate:
				micBase = _resamplePcm16(micBase, 2, micRate, mixRate)
		if sysBase is None:
			sysBase = bytearray(sysData)
			if sysRate != mixRate:
				sysBase = _resamplePcm16(sysBase, 2, sysRate, mixRate)
		micGained = _applyGainDb(micBase, 2, micGainDb)
		sysGained = _applyGainDb(sysBase, 2, sysGainDb)
		current = _mixStereoTracks(micGained, sysGained)
		return micBase, sysBase, current, 2, mixRate
	if micData is not None:
		return None, None, bytearray(micData), micChannels, micRate
	if sysData is not None:
		return None, None, bytearray(sysData), 2, sysRate
	return None, None, bytearray(), 2, 44100


def _mixStereoTracks(dataA, dataB):
	"""Sums two 16-bit stereo PCM buffers sample-by-sample, clipping on
	overflow, padding the shorter one with silence so both tracks stay
	aligned in time rather than one abruptly cutting out. Uses
	audioop.add (C-level, and already clips on overflow) when available."""
	trimmedA = bytes(dataA[:len(dataA) - (len(dataA) % 2)])
	trimmedB = bytes(dataB[:len(dataB) - (len(dataB) % 2)])
	length = max(len(trimmedA), len(trimmedB))
	if audioop is not None:
		trimmedA = trimmedA + b"\x00" * (length - len(trimmedA))
		trimmedB = trimmedB + b"\x00" * (length - len(trimmedB))
		return bytearray(audioop.add(trimmedA, trimmedB, 2))
	a = array.array("h")
	a.frombytes(trimmedA)
	b = array.array("h")
	b.frombytes(trimmedB)
	out = array.array("h", bytes(length))
	for i in range(length // 2):
		va = a[i] if i < len(a) else 0
		vb = b[i] if i < len(b) else 0
		s = va + vb
		if s > 32767:
			s = 32767
		elif s < -32768:
			s = -32768
		out[i] = s
	return bytearray(out.tobytes())


def _applyGainDb(data, sampwidth, dbChange):
	"""Multiplies every sample by the linear factor equivalent to dbChange
	decibels, clipping instead of wrapping on overflow. Uses the stdlib
	audioop module (fast, C-level) when available, falling back to a plain
	Python loop otherwise."""
	factor = 10 ** (dbChange / 20.0)
	if audioop is not None:
		return bytearray(audioop.mul(bytes(data), sampwidth, factor))
	out = bytearray(len(data))
	maxVal = (1 << (8 * sampwidth - 1)) - 1
	minVal = -(1 << (8 * sampwidth - 1))
	for i in range(0, len(data) - sampwidth + 1, sampwidth):
		v = int.from_bytes(data[i:i + sampwidth], "little", signed=True)
		v = max(minVal, min(maxVal, int(v * factor)))
		out[i:i + sampwidth] = v.to_bytes(sampwidth, "little", signed=True)
	return out


def _normalizeGainDb(data, sampwidth, targetPeakRatio=0.98):
	"""Returns the dB gain needed to bring the loudest sample up to
	targetPeakRatio of full scale (the most a recording can be boosted
	without clipping), or 0 if it's already there or the clip is silent."""
	if audioop is not None:
		peak = audioop.max(bytes(data), sampwidth)
	else:
		peak = 0
		for i in range(0, len(data) - sampwidth + 1, sampwidth):
			v = abs(int.from_bytes(data[i:i + sampwidth], "little", signed=True))
			if v > peak:
				peak = v
	if peak <= 0:
		return 0.0
	maxVal = (1 << (8 * sampwidth - 1)) - 1
	target = maxVal * targetPeakRatio
	if peak >= target:
		return 0.0
	return 20 * math.log10(target / peak)


def _removeSilence(data, channels, sampwidth, framerate, thresholdRatio=0.15, minSilenceMs=600, keepPaddingMs=250, windowMs=20):
	"""Trims silence from a recording: leading/trailing silence is cut
	entirely, long internal gaps are shortened to a natural-sounding
	length rather than removed, and short pauses are left alone. Returns
	the new audio and how many seconds were removed.

	Loudness is measured via RMS per window, not peak, and the threshold
	comes from a high percentile of those RMS values rather than the
	recording's absolute peak - a single loud sample (click, pop, mic
	bump) no longer makes a whole window, or the whole recording's
	reference level, look "loud", which was causing real silence to be
	missed throughout."""
	frameSize = channels * sampwidth
	totalFrames = len(data) // frameSize
	if totalFrames == 0:
		return bytearray(data), 0.0
	windowFrames = max(1, int(framerate * windowMs / 1000.0))
	windowBytes = windowFrames * frameSize

	windowRms = []
	for offset in range(0, len(data), windowBytes):
		chunk = bytes(data[offset:offset + windowBytes])
		if not chunk or len(chunk) < sampwidth:
			break
		if audioop is not None:
			windowRms.append(audioop.rms(chunk, sampwidth))
		else:
			total = 0
			count = 0
			for i in range(0, len(chunk) - sampwidth + 1, sampwidth):
				v = int.from_bytes(chunk[i:i + sampwidth], "little", signed=True)
				total += v * v
				count += 1
			windowRms.append(int((total / count) ** 0.5) if count else 0)
	if not windowRms:
		return bytearray(data), 0.0

	sortedRms = sorted(windowRms)
	loudRef = sortedRms[int(0.95 * (len(sortedRms) - 1))]
	if loudRef <= 0:
		return bytearray(data), 0.0
	threshold = max(1, int(loudRef * thresholdRatio))
	loudFlags = [r >= threshold for r in windowRms]

	# Smooth out brief noise blips (shorter than ~60ms) that are surrounded
	# by silence, so a pop or click doesn't fragment one long silence into
	# many short ones that individually never reach the minimum length
	# needed to be trimmed.
	minLoudRun = max(1, int(60 / windowMs))
	i = 0
	n = len(loudFlags)
	while i < n:
		if loudFlags[i]:
			j = i
			while j < n and loudFlags[j]:
				j += 1
			if j - i < minLoudRun:
				for k in range(i, j):
					loudFlags[k] = False
			i = j
		else:
			i += 1

	runs = []
	curVal = loudFlags[0]
	runStart = 0
	for i in range(1, len(loudFlags)):
		if loudFlags[i] != curVal:
			runs.append((curVal, runStart, i))
			runStart = i
			curVal = loudFlags[i]
	runs.append((curVal, runStart, len(loudFlags)))

	minSilenceWindows = max(1, int(minSilenceMs / windowMs))
	minEdgeSilenceWindows = max(1, int(150 / windowMs))
	keepPaddingWindows = max(0, int(keepPaddingMs / windowMs))
	keepRanges = []
	removedFrames = 0
	for idx, (isLoud, startW, endW) in enumerate(runs):
		startFrame = startW * windowFrames
		endFrame = min(totalFrames, endW * windowFrames)
		if isLoud:
			keepRanges.append((startFrame, endFrame))
			continue
		runLenWindows = endW - startW
		isEdge = idx == 0 or idx == len(runs) - 1
		if isEdge:
			if runLenWindows < minEdgeSilenceWindows:
				keepRanges.append((startFrame, endFrame))
			else:
				removedFrames += endFrame - startFrame
			continue
		if runLenWindows < minSilenceWindows:
			keepRanges.append((startFrame, endFrame))
			continue
		keepWindows = min(runLenWindows, keepPaddingWindows)
		if keepWindows <= 0:
			removedFrames += endFrame - startFrame
			continue
		halfWindows = keepWindows // 2
		keepEndFrame = min(endFrame, startFrame + halfWindows * windowFrames)
		keepRanges.append((startFrame, keepEndFrame))
		tailStartFrame = max(keepEndFrame, endFrame - (keepWindows - halfWindows) * windowFrames)
		if tailStartFrame < endFrame:
			keepRanges.append((tailStartFrame, endFrame))
		removedFrames += (endFrame - startFrame) - (keepEndFrame - startFrame) - (endFrame - tailStartFrame)

	newData = bytearray()
	for start, end in keepRanges:
		newData.extend(data[start * frameSize:end * frameSize])
	return newData, removedFrames / float(framerate)


def _muteSilence(data, channels, sampwidth, framerate, thresholdRatio=0.15, minSilenceMs=600, keepPaddingMs=250, windowMs=20):
	"""Same silence detection as _removeSilence, but instead of cutting
	silent stretches out, it zeroes them in place - the clip's length
	(and therefore its timing) never changes. Used when a microphone
	track was recorded alongside computer audio, where trimming the
	microphone would pull it out of sync with the other track."""
	frameSize = channels * sampwidth
	totalFrames = len(data) // frameSize
	if totalFrames == 0:
		return bytearray(data), 0.0
	windowFrames = max(1, int(framerate * windowMs / 1000.0))
	windowBytes = windowFrames * frameSize

	windowRms = []
	for offset in range(0, len(data), windowBytes):
		chunk = bytes(data[offset:offset + windowBytes])
		if not chunk or len(chunk) < sampwidth:
			break
		if audioop is not None:
			windowRms.append(audioop.rms(chunk, sampwidth))
		else:
			total = 0
			count = 0
			for i in range(0, len(chunk) - sampwidth + 1, sampwidth):
				v = int.from_bytes(chunk[i:i + sampwidth], "little", signed=True)
				total += v * v
				count += 1
			windowRms.append(int((total / count) ** 0.5) if count else 0)
	if not windowRms:
		return bytearray(data), 0.0

	sortedRms = sorted(windowRms)
	loudRef = sortedRms[int(0.95 * (len(sortedRms) - 1))]
	if loudRef <= 0:
		return bytearray(data), 0.0
	threshold = max(1, int(loudRef * thresholdRatio))
	loudFlags = [r >= threshold for r in windowRms]

	minLoudRun = max(1, int(60 / windowMs))
	i = 0
	n = len(loudFlags)
	while i < n:
		if loudFlags[i]:
			j = i
			while j < n and loudFlags[j]:
				j += 1
			if j - i < minLoudRun:
				for k in range(i, j):
					loudFlags[k] = False
			i = j
		else:
			i += 1

	runs = []
	curVal = loudFlags[0]
	runStart = 0
	for i in range(1, len(loudFlags)):
		if loudFlags[i] != curVal:
			runs.append((curVal, runStart, i))
			runStart = i
			curVal = loudFlags[i]
	runs.append((curVal, runStart, len(loudFlags)))

	minSilenceWindows = max(1, int(minSilenceMs / windowMs))
	minEdgeSilenceWindows = max(1, int(150 / windowMs))
	keepPaddingWindows = max(0, int(keepPaddingMs / windowMs))
	newData = bytearray(data)
	mutedFrames = 0
	for idx, (isLoud, startW, endW) in enumerate(runs):
		if isLoud:
			continue
		startFrame = startW * windowFrames
		endFrame = min(totalFrames, endW * windowFrames)
		runLenWindows = endW - startW
		isEdge = idx == 0 or idx == len(runs) - 1
		if isEdge:
			if runLenWindows < minEdgeSilenceWindows:
				continue
			muteStart, muteEnd = startFrame, endFrame
		else:
			if runLenWindows < minSilenceWindows:
				continue
			keepWindows = min(runLenWindows, keepPaddingWindows)
			halfWindows = keepWindows // 2
			muteStart = min(endFrame, startFrame + halfWindows * windowFrames)
			muteEnd = max(muteStart, endFrame - (keepWindows - halfWindows) * windowFrames)
		if muteEnd > muteStart:
			newData[muteStart * frameSize:muteEnd * frameSize] = bytes((muteEnd - muteStart) * frameSize)
			mutedFrames += muteEnd - muteStart
	return newData, mutedFrames / float(framerate)


NOISE_REDUCTION_NR_LEVELS = {
	"light": 12,
	"medium": 20,
	"aggressive": 35,
}

# How far above the recording's own measured noise floor to set ffmpeg's
# nf (noise floor) parameter. afftdn barely reduces anything if nf is left
# at its default (-50dB) on a typical mic recording, whose real noise
# floor is often well above that - so nf has to be derived from the
# actual recording, not guessed. The margin is what sensitivity controls.
NOISE_REDUCTION_NF_MARGIN_DB = {
	"light": -6.0,
	"medium": 2.0,
	"aggressive": 9.0,
}


def _estimateNoiseFloorDb(data, channels, sampwidth, framerate, windowMs=20):
	"""Estimates the recording's background noise level in dBFS from the
	quietest ~15% of short windows - the same idea _removeSilence uses to
	spot gaps, just turned into a level instead of a set of time ranges.
	Uses audioop (C-level) so it's cheap even on long recordings. Used to
	tell ffmpeg's afftdn filter roughly where this specific recording's
	noise floor actually sits, since its default assumption is usually
	too conservative for a typical mic."""
	if audioop is None or sampwidth != 2:
		return -50.0
	frameSize = channels * sampwidth
	windowBytes = max(frameSize, int(framerate * windowMs / 1000.0) * frameSize)
	rmsValues = []
	for offset in range(0, len(data) - windowBytes + 1, windowBytes):
		chunk = bytes(data[offset:offset + windowBytes])
		if len(chunk) < sampwidth:
			break
		rmsValues.append(audioop.rms(chunk, sampwidth))
	if not rmsValues:
		return -50.0
	sortedRms = sorted(rmsValues)
	quietCount = max(1, int(len(sortedRms) * 0.15))
	quietAvg = sum(sortedRms[:quietCount]) / quietCount
	if quietAvg <= 0:
		return -50.0
	return 20 * math.log10(quietAvg / 32768.0)


def _denoiseWithFfmpeg(ffmpegPath, wavPath, outPath, nrLevel, nfDb):
	"""Runs ffmpeg's afftdn (adaptive FFT noise reduction) on wavPath,
	writing to outPath. Same spectral-subtraction idea as _reduceNoise
	below, but via ffmpeg's compiled C code instead of a pure-Python FFT
	loop, so it finishes in about the time it takes to read the file.
	nfDb should come from _estimateNoiseFloorDb - afftdn's own default
	is usually too conservative. Used when ffmpeg is available; falls
	back to _reduceNoise otherwise."""
	nfDb = max(-80.0, min(-20.0, nfDb))
	args = [
		ffmpegPath, "-y", "-i", wavPath,
		"-af", "afftdn=nr=%d:nf=%.1f:nt=w" % (nrLevel, nfDb),
		outPath,
	]
	result = _runFfmpeg(args)
	if result.returncode != 0 or not os.path.exists(outPath):
		raise Exception((result.stdout or b"").decode("utf-8", errors="replace")[-300:])


def _estimateNoiseReductionDb(beforeData, afterData, channels, sampwidth, framerate, windowMs=20):
	"""Cheap RMS-based estimate of how much a noise-reduction pass
	lowered the noise floor, for the status message. Finds the quietest
	~15% of windows in the original audio (the same idea _removeSilence
	uses to spot gaps) and compares their average RMS before and after.
	Uses audioop (C-level) throughout, so unlike the pure-Python spectral
	analysis this stays fast regardless of how the noise reduction itself
	was done."""
	if audioop is None:
		return 0.0
	frameSize = channels * sampwidth
	windowBytes = max(frameSize, int(framerate * windowMs / 1000.0) * frameSize)
	length = min(len(beforeData), len(afterData))
	beforeRms = []
	afterRms = []
	for offset in range(0, length - windowBytes + 1, windowBytes):
		beforeChunk = bytes(beforeData[offset:offset + windowBytes])
		afterChunk = bytes(afterData[offset:offset + windowBytes])
		if len(beforeChunk) < sampwidth or len(afterChunk) < sampwidth:
			break
		beforeRms.append(audioop.rms(beforeChunk, sampwidth))
		afterRms.append(audioop.rms(afterChunk, sampwidth))
	if not beforeRms:
		return 0.0
	quietCount = max(1, int(len(beforeRms) * 0.15))
	quietIdx = sorted(range(len(beforeRms)), key=lambda i: beforeRms[i])[:quietCount]
	beforeAvg = sum(beforeRms[i] for i in quietIdx) / len(quietIdx)
	afterAvg = sum(afterRms[i] for i in quietIdx) / len(quietIdx)
	if beforeAvg <= 0 or afterAvg <= 0:
		return 0.0
	return 20 * math.log10(beforeAvg / afterAvg)


NOISE_REDUCTION_PRESETS = {
	"light": dict(oversubtraction=1.3, spectralFloor=0.25),
	"medium": dict(oversubtraction=2.0, spectralFloor=0.12),
	"aggressive": dict(oversubtraction=3.2, spectralFloor=0.04),
}

_NR_FRAME_SIZE = 1024
_NR_HOP = _NR_FRAME_SIZE // 2


def _periodicHann(n):
	"""A Hann window that sums to exactly 1.0 when overlap-added at 50%
	hop, so short-time frames can be reconstructed by simple addition
	without a separate normalization pass."""
	return [0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)]


def _fftInPlace(a):
	"""Iterative radix-2 Cooley-Tukey FFT. len(a) must be a power of two.
	Mutates and returns the list of complex numbers passed in."""
	n = len(a)
	j = 0
	for i in range(1, n):
		bit = n >> 1
		while j & bit:
			j ^= bit
			bit >>= 1
		j ^= bit
		if i < j:
			a[i], a[j] = a[j], a[i]
	length = 2
	while length <= n:
		half = length // 2
		ang = -2 * math.pi / length
		wlen = complex(math.cos(ang), math.sin(ang))
		for i in range(0, n, length):
			w = complex(1.0, 0.0)
			for k in range(i, i + half):
				u = a[k]
				v = a[k + half] * w
				a[k] = u + v
				a[k + half] = u - v
				w *= wlen
		length <<= 1
	return a


def _ifftInPlace(a):
	n = len(a)
	for i in range(n):
		a[i] = a[i].conjugate()
	_fftInPlace(a)
	for i in range(n):
		a[i] = a[i].conjugate() / n
	return a


def _reduceNoise(data, channels, sampwidth, framerate, oversubtraction=2.0, spectralFloor=0.12):
	"""Reduces steady background noise (hiss, hum, fan/AC rumble) via
	frequency-domain spectral subtraction. The recording is split into
	overlapping windows, a noise profile is estimated from the quietest
	~15% of them, and that profile is subtracted from every window's
	magnitude spectrum (keeping the original phase) before rebuilding.

	Unlike silence removal, nothing is cut: the noise floor drops
	everywhere, including under speech, and length is unchanged. Returns
	the processed audio and the average noise-floor reduction in dB
	(0.0 if nothing could be estimated)."""
	if sampwidth != 2:
		# This add-on's recorder only ever produces 16-bit audio; guard
		# rather than risk misinterpreting sample width elsewhere.
		return bytearray(data), 0.0
	frameSize = _NR_FRAME_SIZE
	hop = _NR_HOP
	totalSamples = len(data) // sampwidth
	totalFrames = totalSamples // channels
	if totalFrames < frameSize:
		return bytearray(data), 0.0

	allSamples = array.array("h")
	allSamples.frombytes(bytes(data[:totalFrames * channels * sampwidth]))

	window = _periodicHann(frameSize)
	numBins = frameSize // 2 + 1
	half = frameSize // 2

	channelOutputs = []
	totalReductionDb = 0.0
	reductionSamples = 0

	for ch in range(channels):
		samples = [allSamples[i * channels + ch] / 32768.0 for i in range(totalFrames)]

		hopStarts = list(range(0, totalFrames - frameSize + 1, hop))
		if not hopStarts:
			hopStarts = [0]
		elif hopStarts[-1] + frameSize < totalFrames:
			hopStarts.append(totalFrames - frameSize)

		# First pass: analyse every frame's spectrum and loudness.
		spectra = []
		rmsValues = []
		for start in hopStarts:
			frame = [samples[start + i] * window[i] for i in range(frameSize)]
			spectra.append(_fftInPlace([complex(v, 0.0) for v in frame]))
			energy = sum(v * v for v in frame)
			rmsValues.append((energy / frameSize) ** 0.5)

		sortedRms = sorted(rmsValues)
		if sortedRms[-1] <= 0.0:
			# Silent channel - nothing to reduce.
			channelOutputs.append(samples)
			continue
		quietCount = max(1, int(len(rmsValues) * 0.15))
		quietThreshold = sortedRms[quietCount - 1]
		noiseIndices = [i for i, r in enumerate(rmsValues) if r <= quietThreshold]
		if not noiseIndices:
			noiseIndices = [rmsValues.index(sortedRms[0])]

		noiseProfile = [0.0] * numBins
		for idx in noiseIndices:
			spec = spectra[idx]
			for b in range(numBins):
				noiseProfile[b] += abs(spec[b])
		noiseProfile = [v / len(noiseIndices) for v in noiseProfile]

		# Second pass: subtract the noise profile from every frame's
		# magnitude, keep the original phase, and overlap-add the result
		# back into the output. The periodic Hann window sums to exactly
		# 1 across 50%-overlapped frames, so plain addition reconstructs
		# the signal correctly without extra normalization.
		output = [0.0] * totalFrames
		for frameIdx, start in enumerate(hopStarts):
			spec = spectra[frameIdx]
			newSpec = [0j] * frameSize
			for b in range(numBins):
				mag = abs(spec[b])
				phase = cmath.phase(spec[b])
				reduced = mag - oversubtraction * noiseProfile[b]
				floor = spectralFloor * mag
				newMag = reduced if reduced > floor else floor
				newSpec[b] = cmath.rect(newMag, phase)
			for b in range(1, half):
				newSpec[frameSize - b] = newSpec[b].conjugate()
			timeFrame = _ifftInPlace(newSpec)
			for i in range(frameSize):
				output[start + i] += timeFrame[i].real

		# Track how much the average level of the noise windows actually
		# dropped, for the status message.
		beforeRms = sum(rmsValues[i] for i in noiseIndices) / len(noiseIndices)
		afterEnergy = 0.0
		for idx in noiseIndices:
			start = hopStarts[idx]
			afterEnergy += sum((output[start + i] * window[i]) ** 2 for i in range(frameSize))
		afterRms = (afterEnergy / (len(noiseIndices) * frameSize)) ** 0.5
		if beforeRms > 0 and afterRms > 0:
			totalReductionDb += 20 * math.log10(beforeRms / afterRms)
			reductionSamples += 1
		elif beforeRms > 0:
			totalReductionDb += 40.0
			reductionSamples += 1

		channelOutputs.append(output)

	outByteLen = totalFrames * channels * sampwidth
	outSamples = array.array("h", bytes(outByteLen))
	for i in range(totalFrames):
		for ch in range(channels):
			v = channelOutputs[ch][i]
			iv = int(v * 32768.0)
			if iv > 32767:
				iv = 32767
			elif iv < -32768:
				iv = -32768
			outSamples[i * channels + ch] = iv

	avgReductionDb = (totalReductionDb / reductionSamples) if reductionSamples else 0.0
	return bytearray(outSamples.tobytes()), avgReductionDb


def _loadHistory():
	path = _getHistoryFilePath()
	try:
		with open(path, "r", encoding="utf-8") as f:
			data = json.load(f)
		if isinstance(data, list):
			return data
	except FileNotFoundError:
		pass
	except Exception:
		log.error("Cloud Uploader: could not read link history")
	return []


def _getMaxHistoryEntries():
	try:
		return config.conf["cloudUploader"]["maxHistoryEntries"]
	except Exception:
		return HISTORY_MAX_ENTRIES_DEFAULT


def _saveHistory(history):
	try:
		with open(_getHistoryFilePath(), "w", encoding="utf-8") as f:
			json.dump(history[:_getMaxHistoryEntries()], f)
	except Exception:
		log.error("Cloud Uploader: could not save link history")


def _pruneExpired(history):
	now = datetime.datetime.now()
	kept = []
	for entry in history:
		try:
			if datetime.datetime.fromisoformat(entry["expiresAt"]) > now:
				kept.append(entry)
		except Exception:
			kept.append(entry)
	return kept


def _formatSince(pastIso, now):
	try:
		seconds = (now - datetime.datetime.fromisoformat(pastIso)).total_seconds()
	except Exception:
		return ""
	if seconds < 60:
		return _("just now")
	minutes = int(seconds // 60)
	if minutes < 60:
		return _("{n} min ago").format(n=minutes)
	hours = int(minutes // 60)
	if hours < 24:
		return _("{n} h ago").format(n=hours)
	days = int(hours // 24)
	return _("{n} d ago").format(n=days)


def _formatUntil(futureIso, now):
	try:
		seconds = (datetime.datetime.fromisoformat(futureIso) - now).total_seconds()
	except Exception:
		return ""
	if seconds <= 0:
		return _("expired")
	minutes = max(1, int(seconds // 60))
	if minutes < 60:
		return _("in {n} min").format(n=minutes)
	hours = int(minutes // 60)
	if hours < 24:
		return _("in {n} h").format(n=hours)
	days = int(hours // 24)
	return _("in {n} d").format(n=days)


class UploadCancelledError(Exception):
	pass


class _CancelToken(object):
	"""Wraps a threading.Event, but also forcibly aborts whatever HTTP
	connection is currently registered with it the moment cancellation is
	requested. Without this, clicking Cancel while a request is blocked
	waiting on the server (e.g. inside getresponse(), after the whole file
	has already been sent) does nothing until the socket timeout expires,
	since nothing in that wait loop ever rechecks the cancel flag."""

	def __init__(self):
		self._event = threading.Event()
		self._lock = threading.Lock()
		self._conn = None

	def is_set(self):
		return self._event.is_set()

	def set(self):
		self._event.set()
		with self._lock:
			conn = self._conn
		if conn is not None:
			sock = getattr(conn, "sock", None)
			if sock is not None:
				try:
					sock.shutdown(socket.SHUT_RDWR)
				except Exception:
					pass
			try:
				conn.close()
			except Exception:
				pass

	def registerConnection(self, conn):
		with self._lock:
			self._conn = conn

	def clearConnection(self):
		with self._lock:
			self._conn = None


class _CancellableHTTPSConnection(http.client.HTTPSConnection):
	"""An HTTPSConnection with a pollable, cancellable connect() step,
	so cancelEvent can abort a connection attempt in progress."""

	def __init__(self, host, cancelEvent, connectTimeout=10, pollInterval=0.3, **kwargs):
		super().__init__(host, **kwargs)
		self._cancelToken = cancelEvent
		self._connectTimeout = connectTimeout
		self._pollInterval = pollInterval

	def connect(self):
		cancelEvent = self._cancelToken
		lastErr = None
		for family, socktype, proto, canonname, sockaddr in socket.getaddrinfo(
			self.host, self.port, 0, socket.SOCK_STREAM
		):
			if cancelEvent.is_set():
				raise UploadCancelledError()
			sock = socket.socket(family, socktype, proto)
			try:
				sock.setblocking(False)
				try:
					sock.connect(sockaddr)
				except BlockingIOError:
					pass
				waited = 0.0
				connected = False
				while waited < self._connectTimeout:
					if cancelEvent.is_set():
						sock.close()
						raise UploadCancelledError()
					_, writable, errored = select.select([], [sock], [sock], self._pollInterval)
					if errored:
						break
					if writable:
						err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
						connected = err == 0
						break
					waited += self._pollInterval
				if not connected:
					sock.close()
					lastErr = OSError(_("Could not connect to the upload server."))
					continue
				sock.setblocking(True)
				# The TLS handshake itself is a normal blocking call, but
				# bounded to connectTimeout rather than left unbounded.
				sock.settimeout(self._connectTimeout)
				self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
				self.sock.settimeout(30)
				return
			except UploadCancelledError:
				raise
			except Exception as e:
				lastErr = e
				try:
					sock.close()
				except Exception:
					pass
				continue
		if lastErr is not None:
			raise lastErr
		raise OSError(_("Could not connect to the upload server."))


class _MultipartStream(object):
	"""Streams a multipart/form-data body from a file plus extra fields
	without loading the whole file into memory, reporting progress."""

	def __init__(self, filePath, fieldName, extraFields, progressCallback, cancelEvent):
		self._file = open(filePath, "rb")
		self._fileSize = os.path.getsize(filePath)
		self._progressCallback = progressCallback
		self._cancelEvent = cancelEvent
		boundary = uuid.uuid4().hex
		fileName = os.path.basename(filePath)
		mimeType = mimetypes.guess_type(fileName)[0] or "application/octet-stream"
		preamble = ""
		for key, value in extraFields.items():
			preamble += "--%s\r\n" % boundary
			preamble += 'Content-Disposition: form-data; name="%s"\r\n\r\n' % key
			preamble += "%s\r\n" % value
		preamble += "--%s\r\n" % boundary
		preamble += 'Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (fieldName, fileName)
		preamble += "Content-Type: %s\r\n\r\n" % mimeType
		self._preamble = preamble.encode("utf-8")
		self._epilogue = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
		self.totalSize = len(self._preamble) + self._fileSize + len(self._epilogue)
		self.contentType = "multipart/form-data; boundary=%s" % boundary
		self._sent = 0
		self._stage = 0
		self._lastPercent = -1

	def read(self, size=65536):
		if self._cancelEvent.is_set():
			raise UploadCancelledError()
		data = b""
		if self._stage == 0:
			data = self._preamble
			self._stage = 1
		elif self._stage == 1:
			data = self._file.read(size)
			if not data:
				self._stage = 2
		if self._stage == 2 and not data:
			data = self._epilogue
			self._stage = 3
		if data:
			self._sent += len(data)
			if self._progressCallback and self.totalSize:
				percent = min(100, int(self._sent * 100 / self.totalSize))
				if percent != self._lastPercent:
					self._lastPercent = percent
					self._progressCallback(percent)
		return data

	def close(self):
		try:
			self._file.close()
		except Exception:
			pass


class _RawStream(object):
	"""Streams a plain file body (no multipart framing), reporting
	progress. Used for hosts that accept the file as a raw POST/PUT body,
	such as Filebin."""

	def __init__(self, filePath, progressCallback, cancelEvent):
		self._file = open(filePath, "rb")
		self.totalSize = os.path.getsize(filePath)
		self._progressCallback = progressCallback
		self._cancelEvent = cancelEvent
		self._sent = 0
		self._lastPercent = -1

	def read(self, size=65536):
		if self._cancelEvent.is_set():
			raise UploadCancelledError()
		data = self._file.read(size)
		if data:
			self._sent += len(data)
			if self._progressCallback and self.totalSize:
				percent = min(100, int(self._sent * 100 / self.totalSize))
				if percent != self._lastPercent:
					self._lastPercent = percent
					self._progressCallback(percent)
		return data

	def close(self):
		try:
			self._file.close()
		except Exception:
			pass


def _sendAllWithCancel(sock, data, cancelEvent, chunkTimeout=0.5):
	"""Sends data over sock in a way that notices a cancel request quickly,
	even if the peer stops reading and a plain sendall() would otherwise
	block for the full connection timeout. Applies a short socket timeout
	so each attempt either makes some progress or gives up quickly, and
	rechecks cancelEvent between attempts. This is safe to interrupt and
	retry, since only the count of bytes actually sent can differ - no
	data is ever skipped or resent."""
	view = memoryview(data)
	total = len(view)
	sent = 0
	originalTimeout = sock.gettimeout()
	try:
		sock.settimeout(chunkTimeout)
		while sent < total:
			if cancelEvent.is_set():
				raise UploadCancelledError()
			try:
				n = sock.send(view[sent:])
			except socket.timeout:
				continue
			except OSError:
				raise
			if not n:
				raise OSError("Connection closed by peer while sending")
			sent += n
	finally:
		try:
			sock.settimeout(originalTimeout)
		except Exception:
			pass


def _waitForResponseReady(conn, cancelEvent, pollInterval=0.5, overallTimeout=30):
	"""Polls the connection's socket for readability in short intervals,
	checking cancelEvent between each poll, so a cancel request is noticed
	within about pollInterval seconds instead of only once the full
	response timeout elapses. If the socket isn't reachable for polling
	for any reason, this just returns immediately and getresponse() falls
	back to its own normal (uninterruptible) blocking behavior."""
	sock = getattr(conn, "sock", None)
	if sock is None:
		return
	waited = 0.0
	while waited < overallTimeout:
		if cancelEvent.is_set():
			raise UploadCancelledError()
		try:
			ready = select.select([sock], [], [], pollInterval)[0]
		except Exception:
			return
		if ready:
			return
		waited += pollInterval


def _performUpload(conn, method, path, headers, stream, cancelEvent):
	"""Shared connect/request/response logic used by every host. Sends
	the body in cancellable chunks rather than one blocking call.
	Returns (status, text)."""
	cancelEvent.registerConnection(conn)
	try:
		try:
			conn.putrequest(method, path)
			for key, value in headers.items():
				conn.putheader(key, value)
			conn.endheaders()
			sock = conn.sock
			while True:
				if cancelEvent.is_set():
					raise UploadCancelledError()
				chunk = stream.read(65536)
				if not chunk:
					break
				_sendAllWithCancel(sock, chunk, cancelEvent)
		except UploadCancelledError:
			raise
		except Exception:
			if cancelEvent.is_set():
				raise UploadCancelledError()
			raise Exception(_("Could not connect to the upload server. Please check your internet connection."))
		try:
			_waitForResponseReady(conn, cancelEvent)
			resp = conn.getresponse()
			text = resp.read().decode("utf-8", errors="replace").strip()
		except UploadCancelledError:
			raise
		except Exception:
			if cancelEvent.is_set():
				raise UploadCancelledError()
			raise Exception(_("The connection was lost while uploading. Please check your internet connection and try again."))
		return resp.status, text
	finally:
		cancelEvent.clearConnection()


def _uploadToLitterbox(filePath, timeCode, progressCallback, cancelEvent):
	stream = _MultipartStream(
		filePath, "fileToUpload", {"reqtype": "fileupload", "time": timeCode}, progressCallback, cancelEvent
	)
	conn = None
	try:
		conn = _CancellableHTTPSConnection(UPLOAD_HOST, cancelEvent)
		headers = {
			"Content-Type": stream.contentType,
			"Content-Length": str(stream.totalSize),
			"User-Agent": "Mozilla/5.0 (compatible; NVDA-CloudUploader/1.0)",
		}
		status, text = _performUpload(conn, "POST", UPLOAD_PATH, headers, stream, cancelEvent)
		if status != 200:
			raise Exception(_("The upload server returned an error ({status}): {text}").format(status=status, text=text[:200]))
		if not text.lower().startswith("http"):
			raise Exception(_("The upload server returned an unexpected response: {text}").format(text=text[:200]))
		return text
	finally:
		stream.close()
		if conn is not None:
			try:
				conn.close()
			except Exception:
				pass


def _uploadToGofile(filePath, expiryCode, progressCallback, cancelEvent):
	stream = _MultipartStream(filePath, "file", {}, progressCallback, cancelEvent)
	conn = None
	try:
		conn = _CancellableHTTPSConnection("upload.gofile.io", cancelEvent)
		headers = {
			"Content-Type": stream.contentType,
			"Content-Length": str(stream.totalSize),
			"User-Agent": "Mozilla/5.0 (compatible; NVDA-CloudUploader/1.0)",
		}
		status, text = _performUpload(conn, "POST", "/uploadfile", headers, stream, cancelEvent)
		if status != 200:
			raise Exception(_("The upload server returned an error ({status}): {text}").format(status=status, text=text[:200]))
		try:
			data = json.loads(text)
		except Exception:
			raise Exception(_("The upload server returned an unexpected response: {text}").format(text=text[:200]))
		if data.get("status") != "ok":
			raise Exception(_("The upload server rejected the file: {text}").format(text=text[:200]))
		fileData = data.get("data", {}) or {}
		link = fileData.get("downloadPage") or fileData.get("downloadPageURL") or ""
		if not link.lower().startswith("http"):
			raise Exception(_("The upload server returned an unexpected response: {text}").format(text=text[:200]))
		return link
	finally:
		stream.close()
		if conn is not None:
			try:
				conn.close()
			except Exception:
				pass


def _uploadToCatbox(filePath, expiryCode, progressCallback, cancelEvent):
	stream = _MultipartStream(filePath, "fileToUpload", {"reqtype": "fileupload"}, progressCallback, cancelEvent)
	conn = None
	try:
		conn = _CancellableHTTPSConnection("catbox.moe", cancelEvent)
		headers = {
			"Content-Type": stream.contentType,
			"Content-Length": str(stream.totalSize),
			"User-Agent": "Mozilla/5.0 (compatible; NVDA-CloudUploader/1.0)",
		}
		status, text = _performUpload(conn, "POST", "/user/api.php", headers, stream, cancelEvent)
		if status != 200:
			raise Exception(_("The upload server returned an error ({status}): {text}").format(status=status, text=text[:200]))
		if not text.lower().startswith("http"):
			raise Exception(_("The upload server returned an unexpected response: {text}").format(text=text[:200]))
		return text
	finally:
		stream.close()
		if conn is not None:
			try:
				conn.close()
			except Exception:
				pass


def _uploadToZeroXZero(filePath, expiryCode, progressCallback, cancelEvent):
	stream = _MultipartStream(filePath, "file", {}, progressCallback, cancelEvent)
	conn = None
	try:
		conn = _CancellableHTTPSConnection("0x0.st", cancelEvent)
		headers = {
			"Content-Type": stream.contentType,
			"Content-Length": str(stream.totalSize),
			"User-Agent": "Mozilla/5.0 (compatible; NVDA-CloudUploader/1.0)",
		}
		status, text = _performUpload(conn, "POST", "/", headers, stream, cancelEvent)
		if status not in (200, 201):
			raise Exception(_("The upload server returned an error ({status}): {text}").format(status=status, text=text[:200]))
		firstLine = text.splitlines()[0].strip() if text else ""
		if not firstLine.lower().startswith("http"):
			raise Exception(_("The upload server returned an unexpected response: {text}").format(text=text[:200]))
		return firstLine
	finally:
		stream.close()
		if conn is not None:
			try:
				conn.close()
			except Exception:
				pass


def _uploadToFilebin(filePath, expiryCode, progressCallback, cancelEvent):
	fileName = os.path.basename(filePath)
	binId = uuid.uuid4().hex[:16]
	stream = _RawStream(filePath, progressCallback, cancelEvent)
	conn = None
	try:
		conn = _CancellableHTTPSConnection("filebin.net", cancelEvent)
		headers = {
			"Content-Type": "application/octet-stream",
			"Content-Length": str(stream.totalSize),
			"User-Agent": "Mozilla/5.0 (compatible; NVDA-CloudUploader/1.0)",
		}
		path = "/%s/%s" % (binId, urllib.parse.quote(fileName))
		status, text = _performUpload(conn, "POST", path, headers, stream, cancelEvent)
		if status not in (200, 201):
			raise Exception(_("The upload server returned an error ({status}): {text}").format(status=status, text=text[:200]))
		return "https://filebin.net/%s/%s" % (binId, urllib.parse.quote(fileName))
	finally:
		stream.close()
		if conn is not None:
			try:
				conn.close()
			except Exception:
				pass


def _uploadToUguu(filePath, expiryCode, progressCallback, cancelEvent):
	stream = _MultipartStream(filePath, "files[]", {}, progressCallback, cancelEvent)
	conn = None
	try:
		conn = _CancellableHTTPSConnection("uguu.se", cancelEvent)
		headers = {
			"Content-Type": stream.contentType,
			"Content-Length": str(stream.totalSize),
			"User-Agent": "Mozilla/5.0 (compatible; NVDA-CloudUploader/1.0)",
		}
		status, text = _performUpload(conn, "POST", "/upload?output=text", headers, stream, cancelEvent)
		if status != 200:
			raise Exception(_("The upload server returned an error ({status}): {text}").format(status=status, text=text[:200]))
		firstLine = text.splitlines()[0].strip() if text else ""
		if not firstLine.lower().startswith("http"):
			raise Exception(_("The upload server returned an unexpected response: {text}").format(text=text[:200]))
		return firstLine
	finally:
		stream.close()
		if conn is not None:
			try:
				conn.close()
			except Exception:
				pass


def _checkHostReachable(hostName, path="/", timeout=3):
	"""Returns True if hostName responds to an HTTP request at all, even
	with an error status - we're only checking whether the server is up,
	not whether the specific endpoint would accept an upload."""
	conn = None
	try:
		conn = http.client.HTTPSConnection(hostName, timeout=timeout)
		conn.request("HEAD", path, headers={"User-Agent": "Mozilla/5.0 (compatible; NVDA-CloudUploader/1.0)"})
		resp = conn.getresponse()
		resp.read()
		return True
	except Exception:
		return False
	finally:
		if conn is not None:
			try:
				conn.close()
			except Exception:
				pass


def _checkAllHosts(hosts, timeout=3):
	"""Checks every host's reachability in parallel rather than one at a
	time, so the total wait stays close to a single timeout instead of
	growing with the number of hosts."""
	results = [None] * len(hosts)

	def worker(i, host):
		results[i] = (host, host.checkStatus(timeout=timeout))

	threads = []
	for i, host in enumerate(hosts):
		t = threading.Thread(target=worker, args=(i, host), daemon=True)
		threads.append(t)
		t.start()
	for t in threads:
		t.join(timeout=timeout + 2)
	for i, host in enumerate(hosts):
		if results[i] is None:
			results[i] = (hosts[i], False)
	return results


class UploadHost(object):
	"""Base class describing an anonymous file host cloud Uploader can send
	files to."""

	key = ""
	label = ""
	expiryOptions = []
	checkHost = ""
	checkPath = "/"

	def upload(self, filePath, expiryCode, progressCallback, cancelEvent):
		raise NotImplementedError

	def checkStatus(self, timeout=3):
		return _checkHostReachable(self.checkHost, self.checkPath, timeout=timeout)


class LitterboxHost(UploadHost):
	key = "litterbox"
	label = _("Litterbox (catbox.moe) - kept 1 hour to 3 days depending on your choice, but renames your file")
	expiryOptions = EXPIRY_OPTIONS
	checkHost = "litterbox.catbox.moe"

	def upload(self, filePath, expiryCode, progressCallback, cancelEvent):
		return _uploadToLitterbox(filePath, expiryCode, progressCallback, cancelEvent)


class GofileHost(UploadHost):
	key = "gofile"
	label = _("Gofile - keeps your original file name on a download page, kept about 10 days")
	expiryOptions = GOFILE_EXPIRY_OPTIONS
	checkHost = "upload.gofile.io"

	def upload(self, filePath, expiryCode, progressCallback, cancelEvent):
		return _uploadToGofile(filePath, expiryCode, progressCallback, cancelEvent)


class CatboxHost(UploadHost):
	key = "catbox"
	label = _("Catbox (catbox.moe) - permanent storage, but renames your file")
	expiryOptions = CATBOX_EXPIRY_OPTIONS
	checkHost = "catbox.moe"

	def upload(self, filePath, expiryCode, progressCallback, cancelEvent):
		return _uploadToCatbox(filePath, expiryCode, progressCallback, cancelEvent)


class ZeroXZeroHost(UploadHost):
	key = "0x0"
	label = _("0x0.st - kept 30 days to 1 year depending on file size, but renames your file")
	expiryOptions = ZEROXZERO_EXPIRY_OPTIONS
	checkHost = "0x0.st"

	def upload(self, filePath, expiryCode, progressCallback, cancelEvent):
		return _uploadToZeroXZero(filePath, expiryCode, progressCallback, cancelEvent)


class FilebinHost(UploadHost):
	key = "filebin"
	label = _("Filebin - keeps your original file name, but not a direct download link, expires in about 6 days")
	expiryOptions = FILEBIN_EXPIRY_OPTIONS
	checkHost = "filebin.net"

	def upload(self, filePath, expiryCode, progressCallback, cancelEvent):
		return _uploadToFilebin(filePath, expiryCode, progressCallback, cancelEvent)


class UguuHost(UploadHost):
	key = "uguu"
	label = _("Uguu - temporary storage, about 48 hours, renames your file")
	expiryOptions = UGUU_EXPIRY_OPTIONS
	checkHost = "uguu.se"

	def upload(self, filePath, expiryCode, progressCallback, cancelEvent):
		return _uploadToUguu(filePath, expiryCode, progressCallback, cancelEvent)


ALL_HOSTS = [LitterboxHost(), GofileHost(), CatboxHost(), ZeroXZeroHost(), FilebinHost(), UguuHost()]
HOSTS_BY_KEY = {host.key: host for host in ALL_HOSTS}


class HostChoiceDialog(wx.Dialog):
	"""Lets the user pick an upload host, with a button to check which
	hosts are currently reachable before deciding."""

	def __init__(self, parent, hosts, autoCheck=False):
		super().__init__(parent, title=_("Upload host"), style=wx.CAPTION)
		self.hosts = hosts
		self._checking = False
		self._displayIndices = list(range(len(hosts)))

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		label = wx.StaticText(self, label=_("Which host should this file be uploaded to?"))
		mainSizer.Add(label, flag=wx.ALL, border=10)

		self.listBox = wx.ListBox(self, choices=[host.label for host in hosts], style=wx.LB_SINGLE)
		self.listBox.SetSelection(0)
		mainSizer.Add(self.listBox, flag=wx.LEFT | wx.RIGHT | wx.EXPAND, border=10, proportion=1)

		self.checkBtn = wx.Button(self, label=_("&Check server status"))
		self.checkBtn.Bind(wx.EVT_BUTTON, self.onCheckStatus)
		mainSizer.Add(self.checkBtn, flag=wx.ALL | wx.ALIGN_CENTER, border=10)

		btnSizer = wx.StdDialogButtonSizer()
		okBtn = wx.Button(self, id=wx.ID_OK, label=_("&OK"))
		okBtn.SetDefault()
		cancelBtn = wx.Button(self, id=wx.ID_CANCEL, label=_("Cancel"))
		cancelBtn.Bind(wx.EVT_BUTTON, self.onCancel)
		btnSizer.AddButton(okBtn)
		btnSizer.AddButton(cancelBtn)
		btnSizer.Realize()
		mainSizer.Add(btnSizer, flag=wx.ALL | wx.ALIGN_CENTER, border=10)

		self.SetSizerAndFit(mainSizer)
		self.SetSize((400, self.GetSize().GetHeight()))
		self.SetEscapeId(wx.ID_CANCEL)
		self.Bind(wx.EVT_CHAR_HOOK, self._onCharHook)
		self.Bind(wx.EVT_CLOSE, self.onCancel)

		if autoCheck:
			wx.CallAfter(self.onCheckStatus, None)

	def _onCharHook(self, evt):
		if evt.GetKeyCode() == wx.WXK_ESCAPE:
			self.EndModal(wx.ID_CANCEL)
			return
		evt.Skip()

	def onCancel(self, evt):
		self.EndModal(wx.ID_CANCEL)
		if hasattr(evt, "Veto"):
			evt.Veto()

	def GetSelection(self):
		sel = self.listBox.GetSelection()
		if sel < 0 or sel >= len(self._displayIndices):
			return -1
		return self._displayIndices[sel]

	def onCheckStatus(self, evt):
		if self._checking:
			return
		self._checking = True
		ui.message(_("Checking server status, please wait..."))
		threading.Thread(target=self._checkThread, daemon=True).start()

	def _checkThread(self):
		results = _checkAllHosts(self.hosts, timeout=3)
		wx.CallAfter(self._applyResults, results)

	def _applyResults(self, results):
		self._checking = False
		onlyWorking = False
		try:
			onlyWorking = config.conf["cloudUploader"]["showOnlyWorkingHosts"]
		except Exception:
			pass
		upCount = sum(1 for host, ok in results if ok)

		def buildLabels(filterToWorking):
			labels = []
			indices = []
			for i, (host, ok) in enumerate(results):
				if filterToWorking and not ok:
					continue
				if ok:
					labels.append(_("{label} - working").format(label=host.label))
				else:
					labels.append(_("{label} - not responding").format(label=host.label))
				indices.append(i)
			return labels, indices

		labels, indices = buildLabels(onlyWorking)
		if not labels:
			# Filtering left nothing (e.g. every host failed) - fall back to
			# showing everything so the dialog isn't left empty.
			labels, indices = buildLabels(False)
		try:
			self.listBox.Set(labels)
			self._displayIndices = indices
			if labels:
				self.listBox.SetSelection(0)
		except RuntimeError:
			return
		# Deferred to its own event-loop turn so it reliably lands after the
		# list content change instead of leaving focus on the check button.
		wx.CallAfter(self._focusListBox)
		ui.message(
			_("Server check complete. {up} of {total} hosts are working.").format(up=upCount, total=len(results))
		)

	def _focusListBox(self):
		try:
			self.listBox.SetFocus()
		except RuntimeError:
			pass


_ID_CHOOSE_ANOTHER_HOST = wx.ID_HIGHEST + 1


class UploadErrorDialog(wx.Dialog):
	"""Shown when an upload fails, offering to retry the same host, switch
	to a different host, or give up."""

	def __init__(self, parent, message):
		super().__init__(parent, title=_("Upload failed"), style=wx.CAPTION)

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		text = wx.StaticText(self, label=message)
		text.Wrap(380)
		mainSizer.Add(text, flag=wx.ALL, border=10)

		retryBtn = wx.Button(self, label=_("&Retry the same host"))
		retryBtn.Bind(wx.EVT_BUTTON, self.onRetry)
		mainSizer.Add(retryBtn, flag=wx.ALL | wx.EXPAND, border=5)

		chooseBtn = wx.Button(self, label=_("&Choose another host"))
		chooseBtn.Bind(wx.EVT_BUTTON, self.onChooseAnother)
		mainSizer.Add(chooseBtn, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=5)

		cancelBtn = wx.Button(self, id=wx.ID_CANCEL, label=_("&Cancel upload"))
		cancelBtn.Bind(wx.EVT_BUTTON, self.onCancel)
		mainSizer.Add(cancelBtn, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=5)

		self.SetSizerAndFit(mainSizer)
		self.SetSize((380, self.GetSize().GetHeight()))
		self.SetEscapeId(wx.ID_CANCEL)
		self.Bind(wx.EVT_CHAR_HOOK, self._onCharHook)
		self.Bind(wx.EVT_CLOSE, self.onCancel)
		retryBtn.SetFocus()

	def _onCharHook(self, evt):
		if evt.GetKeyCode() == wx.WXK_ESCAPE:
			self.EndModal(wx.ID_CANCEL)
			return
		evt.Skip()

	def onRetry(self, evt):
		self.EndModal(wx.ID_RETRY)

	def onChooseAnother(self, evt):
		self.EndModal(_ID_CHOOSE_ANOTHER_HOST)

	def onCancel(self, evt):
		self.EndModal(wx.ID_CANCEL)
		if hasattr(evt, "Veto"):
			evt.Veto()


class LinkDialog(wx.Dialog):
	"""Copy / open / (optionally delete) a single link. Used both right after
	an upload and when activating an item in the history list."""

	def __init__(self, parent, title, message, link, showDelete=False, onDelete=None):
		super().__init__(parent, title=title)
		self.link = link
		self.onDelete = onDelete

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		mainSizer.Add(wx.StaticText(self, label=message), flag=wx.ALL, border=10)
		self.linkCtrl = wx.TextCtrl(self, value=link, style=wx.TE_READONLY, size=(420, -1))
		mainSizer.Add(self.linkCtrl, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		btnSizer = wx.BoxSizer(wx.HORIZONTAL)
		copyBtn = wx.Button(self, label=_("&Copy link"))
		copyBtn.Bind(wx.EVT_BUTTON, self.onCopy)
		btnSizer.Add(copyBtn, flag=wx.ALL, border=5)
		openBtn = wx.Button(self, label=_("&Open in browser"))
		openBtn.Bind(wx.EVT_BUTTON, self.onOpen)
		btnSizer.Add(openBtn, flag=wx.ALL, border=5)
		if showDelete:
			deleteBtn = wx.Button(self, label=_("&Delete"))
			deleteBtn.Bind(wx.EVT_BUTTON, self.onDeleteClicked)
			btnSizer.Add(deleteBtn, flag=wx.ALL, border=5)
		closeBtn = wx.Button(self, label=_("Clos&e"))
		closeBtn.Bind(wx.EVT_BUTTON, self.onClose)
		btnSizer.Add(closeBtn, flag=wx.ALL, border=5)
		mainSizer.Add(btnSizer, flag=wx.ALIGN_CENTER)

		self.SetSizerAndFit(mainSizer)
		self.SetEscapeId(wx.ID_CANCEL)
		self.Bind(wx.EVT_CHAR_HOOK, self._onCharHook)
		copyBtn.SetDefault()
		self.Bind(wx.EVT_SHOW, self._onShow)

	def _onCharHook(self, evt):
		if evt.GetKeyCode() == wx.WXK_ESCAPE:
			self.EndModal(wx.ID_CANCEL)
			return
		evt.Skip()

	def _onShow(self, evt):
		if evt.IsShown():
			self.linkCtrl.SetFocus()
			self.linkCtrl.SelectAll()
		evt.Skip()

	def onCopy(self, evt):
		api.copyToClip(self.link, notify=True)

	def onOpen(self, evt):
		webbrowser.open(self.link)
		ui.message(_("Opening link in your browser"))

	def onClose(self, evt):
		self.EndModal(wx.ID_CANCEL)

	def onDeleteClicked(self, evt):
		if self.onDelete:
			self.onDelete()
		self.EndModal(wx.ID_CANCEL)


class LinkHistoryDialog(wx.Dialog):
	def __init__(self, parent, history, onChange):
		super().__init__(parent, title=_("Upload history"), size=(520, 350))
		self.history = history
		self.onChange = onChange

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		hint = _("Enter for options, control+C to copy, delete to remove. Most recent first.") if history else _("You haven't uploaded any files yet.")
		mainSizer.Add(wx.StaticText(self, label=hint), flag=wx.ALL, border=10)

		self.listCtrl = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
		self.listCtrl.InsertColumn(0, _("File"), width=220)
		self.listCtrl.InsertColumn(1, _("Expires"), width=140)
		self.listCtrl.InsertColumn(2, _("Uploaded"), width=140)
		self._populateList()
		mainSizer.Add(self.listCtrl, proportion=1, flag=wx.LEFT | wx.RIGHT | wx.EXPAND, border=10)
		self.listCtrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.onActivate)
		self.listCtrl.Bind(wx.EVT_KEY_DOWN, self.onKeyDown)

		closeBtn = wx.Button(self, label=_("Clos&e"))
		closeBtn.Bind(wx.EVT_BUTTON, self.onClose)
		mainSizer.Add(closeBtn, flag=wx.ALIGN_CENTER | wx.ALL, border=5)

		self.SetSizerAndFit(mainSizer)
		self.SetEscapeId(wx.ID_CANCEL)
		self.Bind(wx.EVT_CHAR_HOOK, self._onCharHook)
		if history:
			self.listCtrl.SetFocus()
			self.listCtrl.Select(0)
			self.listCtrl.Focus(0)

	def _onCharHook(self, evt):
		if evt.GetKeyCode() == wx.WXK_ESCAPE:
			self.EndModal(wx.ID_CANCEL)
			return
		evt.Skip()

	def onClose(self, evt):
		self.EndModal(wx.ID_CANCEL)

	def _populateList(self):
		self.listCtrl.DeleteAllItems()
		now = datetime.datetime.now()
		for entry in self.history:
			index = self.listCtrl.InsertItem(self.listCtrl.GetItemCount(), entry.get("fileName", ""))
			self.listCtrl.SetItem(index, 1, _formatUntil(entry.get("expiresAt", ""), now))
			self.listCtrl.SetItem(index, 2, _formatSince(entry.get("uploadedAt", ""), now))

	def _getSelectedIndex(self):
		index = self.listCtrl.GetFirstSelected()
		if index == -1:
			ui.message(_("Please select an item first"))
		return index

	def _removeAt(self, index):
		fileName = self.history[index].get("fileName", "")
		del self.history[index]
		self._populateList()
		self.onChange(self.history)
		ui.message(_("Removed {fileName}").format(fileName=fileName))

	def onActivate(self, evt):
		index = evt.GetIndex()
		if not (0 <= index < len(self.history)):
			return
		entry = self.history[index]
		now = datetime.datetime.now()
		gui.mainFrame.prePopup()
		try:
			dlg = LinkDialog(
				self,
				_("Link options"),
				_("{fileName} - {since}, expires {until}").format(
					fileName=entry.get("fileName", ""),
					since=_formatSince(entry.get("uploadedAt", ""), now),
					until=_formatUntil(entry.get("expiresAt", ""), now),
				),
				entry["link"],
				showDelete=True,
				onDelete=lambda: self._removeAt(index),
			)
			dlg.ShowModal()
			wx.CallAfter(dlg.Destroy)
		finally:
			gui.mainFrame.postPopup()

	def onKeyDown(self, evt):
		keyCode = evt.GetKeyCode()
		if keyCode == wx.WXK_DELETE:
			index = self._getSelectedIndex()
			if index != -1:
				self._removeAt(index)
		elif evt.ControlDown() and keyCode == ord("C"):
			index = self._getSelectedIndex()
			if index != -1:
				api.copyToClip(self.history[index]["link"], notify=True)
		else:
			evt.Skip()


class UploadProgressDialog(wx.Dialog):
	"""A minimal progress dialog for uploads.

	Unlike wx.ProgressDialog, updateProgress never pumps the event queue
	internally. wx.ProgressDialog.Update() does that on every call, which
	let code scheduled for when the upload finished run reentrantly while
	an Update() for this same dialog hadn't returned - two paths tearing
	down the same native window at once, crashing NVDA. A plain wx.Gauge
	has no such behavior."""

	def __init__(self, parent, title, message):
		super().__init__(parent, title=title, style=wx.CAPTION)
		self._startTime = time.time()
		self._onCancel = None

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		self.messageCtrl = wx.StaticText(self, label=message)
		mainSizer.Add(self.messageCtrl, flag=wx.ALL | wx.EXPAND, border=10)
		self.gauge = wx.Gauge(self, range=100, size=(300, -1))
		mainSizer.Add(self.gauge, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)
		self.elapsedCtrl = wx.StaticText(self, label=_("Elapsed time: 0:00"))
		mainSizer.Add(self.elapsedCtrl, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
		cancelBtn = wx.Button(self, id=wx.ID_CANCEL, label=_("Cancel"))
		cancelBtn.Bind(wx.EVT_BUTTON, self._onCancelClicked)
		mainSizer.Add(cancelBtn, flag=wx.ALIGN_CENTER | wx.BOTTOM, border=10)

		self.SetSizerAndFit(mainSizer)
		self.CenterOnParent()
		self.Bind(wx.EVT_CLOSE, self._onCancelClicked)

		self._timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._onTimer, self._timer)
		self._timer.Start(1000)

	def setOnCancel(self, callback):
		self._onCancel = callback

	def _onCancelClicked(self, evt):
		if self._onCancel:
			self._onCancel()
		if hasattr(evt, "Veto"):
			evt.Veto()

	def _onTimer(self, evt):
		elapsed = int(time.time() - self._startTime)
		minutes, seconds = divmod(elapsed, 60)
		self.elapsedCtrl.SetLabel(_("Elapsed time: {m}:{s:02d}").format(m=minutes, s=seconds))

	def updateProgress(self, percent, message):
		self.gauge.SetValue(percent)
		self.messageCtrl.SetLabel(message)

	def stopTimer(self):
		if self._timer.IsRunning():
			self._timer.Stop()



class _StreamingPreviewPlayer(object):
	"""Plays mixed PCM from memory through waveOut, applying per-track
	gains on the fly as each buffer is filled. Changing gains takes effect
	on the next buffer (~50ms) with no file rewrite - like a media player."""

	NUM_BUFFERS = 4
	BUFFER_MS = 50
	WHDR_DONE = 0x00000001

	def __init__(self):
		self._handle = None
		self._headers = []
		self._buffers = []
		self._micBase = None
		self._sysBase = None
		self._single = None
		self._micGain = 0.0
		self._sysGain = 0.0
		self._pos = 0  # byte offset into stereo stream
		self._rate = 44100
		self._frameBytes = 4
		self._playing = False
		self._length = 0

	@property
	def isPlaying(self):
		return self._playing

	@property
	def positionMs(self):
		if self._rate <= 0:
			return 0
		return int((self._pos // self._frameBytes) * 1000.0 / self._rate)

	@property
	def lengthMs(self):
		if self._rate <= 0:
			return 0
		return int((self._length // self._frameBytes) * 1000.0 / self._rate)

	def setGains(self, micGainDb, sysGainDb):
		self._micGain = float(micGainDb)
		self._sysGain = float(sysGainDb)

	def start(self, micBase, sysBase, singleData, sampleRate, micGainDb=0.0, sysGainDb=0.0, startMs=0, singleChannels=2):
		self.stop()
		self._micBase = micBase
		self._sysBase = sysBase
		self._micGain = float(micGainDb)
		self._sysGain = float(sysGainDb)
		self._rate = int(sampleRate or 44100)
		self._frameBytes = 4
		# Always feed waveOut stereo PCM. Mono single-source recordings must
		# be upmixed here; playing mono bytes as stereo frames doubles the
		# perceived sample rate (classic chipmunk effect).
		if micBase is not None and sysBase is not None:
			self._single = None
			self._length = min(len(micBase), len(sysBase))
			self._length -= self._length % self._frameBytes
		elif singleData is not None:
			channels = int(singleChannels) if singleChannels else 2
			if channels == 1:
				self._single = _toStereo16(singleData, 1)
			else:
				self._single = bytearray(singleData)
			self._length = len(self._single)
			self._length -= self._length % self._frameBytes
		else:
			self._single = None
			self._length = 0
		self._pos = int(startMs * self._rate / 1000.0) * self._frameBytes
		self._pos = max(0, min(self._pos, max(0, self._length - self._frameBytes)))

		fmt = WAVEFORMATEX()
		fmt.wFormatTag = WAVE_FORMAT_PCM
		fmt.nChannels = 2
		fmt.nSamplesPerSec = self._rate
		fmt.wBitsPerSample = 16
		fmt.nBlockAlign = 4
		fmt.nAvgBytesPerSec = self._rate * 4
		fmt.cbSize = 0
		handle = ctypes.c_void_p()
		result = _winmm.waveOutOpen(
			ctypes.byref(handle), WAVE_MAPPER, ctypes.byref(fmt),
			None, None, CALLBACK_NULL,
		)
		if result != MMSYSERR_NOERROR:
			raise Exception(_("Could not open the audio playback device (error {code})").format(code=result))
		self._handle = handle
		bufBytes = max(self._frameBytes, int(self._rate * self.BUFFER_MS / 1000.0) * self._frameBytes)
		self._headers = []
		self._buffers = []
		for _i in range(self.NUM_BUFFERS):
			buf = ctypes.create_string_buffer(bufBytes)
			header = WAVEHDR()
			header.lpData = ctypes.cast(buf, ctypes.c_void_p)
			header.dwBufferLength = bufBytes
			header.dwFlags = self.WHDR_DONE
			self._buffers.append(buf)
			self._headers.append(header)
		self._playing = True
		for header, buf in zip(self._headers, self._buffers):
			self._fillAndWrite(header, buf)

	def poll(self):
		if not self._playing or self._handle is None:
			return
		for header, buf in zip(self._headers, self._buffers):
			if header.dwFlags & self.WHDR_DONE:
				if self._pos >= self._length:
					continue
				self._fillAndWrite(header, buf)
		if self._pos >= self._length and all(h.dwFlags & self.WHDR_DONE for h in self._headers):
			self.stop()

	def _fillAndWrite(self, header, buf):
		if self._handle is None:
			return
		bufBytes = header.dwBufferLength
		chunk = self._renderChunk(bufBytes)
		if not chunk:
			header.dwFlags = self.WHDR_DONE
			header.dwBufferLength = bufBytes
			return
		# Keep original capacity for later refills.
		capacity = len(buf)
		ctypes.memmove(buf, chunk, len(chunk))
		header.lpData = ctypes.cast(buf, ctypes.c_void_p)
		header.dwBufferLength = len(chunk)
		header.dwFlags = 0
		header.dwBytesRecorded = 0
		_winmm.waveOutUnprepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
		_winmm.waveOutPrepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
		_winmm.waveOutWrite(self._handle, ctypes.byref(header), ctypes.sizeof(header))
		# Restore capacity for next fill sizing.
		header.dwBufferLength = capacity

	def _renderChunk(self, nbytes):
		"""Build the next stereo PCM chunk with current gains applied."""
		import array
		need = nbytes - (nbytes % self._frameBytes)
		if need <= 0 or self._pos >= self._length:
			return b""
		end = min(self._pos + need, self._length)
		n = end - self._pos
		n -= n % self._frameBytes
		if n <= 0:
			return b""
		micFactor = 10.0 ** (self._micGain / 20.0)
		sysFactor = 10.0 ** (self._sysGain / 20.0)
		if self._micBase is not None and self._sysBase is not None:
			micSlice = self._micBase[self._pos:self._pos + n]
			sysSlice = self._sysBase[self._pos:self._pos + n]
			if audioop is not None:
				if abs(micFactor - 1.0) > 0.001:
					micSlice = audioop.mul(bytes(micSlice), 2, micFactor)
				else:
					micSlice = bytes(micSlice)
				if abs(sysFactor - 1.0) > 0.001:
					sysSlice = audioop.mul(bytes(sysSlice), 2, sysFactor)
				else:
					sysSlice = bytes(sysSlice)
				mixed = audioop.add(micSlice, sysSlice, 2)
			else:
				ma = array.array("h")
				ma.frombytes(bytes(micSlice))
				sa = array.array("h")
				sa.frombytes(bytes(sysSlice))
				out = array.array("h", [0] * len(ma))
				for i in range(len(ma)):
					v = int(ma[i] * micFactor + sa[i] * sysFactor)
					if v > 32767:
						v = 32767
					elif v < -32768:
						v = -32768
					out[i] = v
				mixed = out.tobytes()
			self._pos += n
			return mixed
		if self._single is not None:
			# Single track is always stereo PCM here (mono was upmixed in start()).
			src = self._single
			slice_ = src[self._pos:self._pos + n]
			factor = micFactor if abs(micFactor - 1.0) >= abs(sysFactor - 1.0) else sysFactor
			if abs(factor - 1.0) > 0.001 and audioop is not None:
				slice_ = audioop.mul(bytes(slice_), 2, factor)
			else:
				slice_ = bytes(slice_)
			self._pos += n
			return slice_
		self._pos = self._length
		return b""

	def stop(self):
		self._playing = False
		if self._handle is None:
			return
		try:
			_winmm.waveOutReset(self._handle)
		except Exception:
			pass
		for header in self._headers:
			try:
				_winmm.waveOutUnprepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
			except Exception:
				pass
		try:
			_winmm.waveOutClose(self._handle)
		except Exception:
			pass
		self._handle = None
		self._headers = []
		self._buffers = []


class RecordVoiceDialog(wx.Dialog):
	"""Lets the user record from the microphone, computer audio, or both
	at once. Silence removal, noise reduction, and normalizing only ever
	touch the microphone track. When both sources are recorded, mic
	silence is muted in place rather than cut, so the two tracks never
	drift out of sync; their relative volumes can still be balanced
	afterward. Preview always plays the current mixed file and keeps
	playback position across edits. A recording can be discarded or
	renamed here before it's uploaded or kept."""

	def __init__(self, parent, autoStart=False, preload=None):
		super().__init__(parent, title=_("Record voice"), style=wx.CAPTION)
		self._autoStart = bool(autoStart) and preload is None
		self._preload = preload
		self._alias = "nvdaCloudUploaderRec%d" % int(time.time() * 1000)
		self._previewLengthMs = 0
		self._recording = False
		self._hasRecording = False
		self._playing = False
		self._previewPlayer = _StreamingPreviewPlayer()
		self._startTime = None
		self._startClockOffset = 0.0
		self._micRecorder = None
		self._sysRecorder = None
		self._outputPath = os.path.join(_getRecordingsFolder(), "%s.wav" % self._alias)
		self._previewPath = os.path.join(_getRecordingsFolder(), "%s_preview.wav" % self._alias)
		self._micOutputPath = os.path.join(_getRecordingsFolder(), "%s_mic.wav" % self._alias)
		self._sysOutputPath = os.path.join(_getRecordingsFolder(), "%s_system.wav" % self._alias)
		self._currentData = None
		self._currentChannels = 2
		self._mixSampleRate = 44100
		self._micData = None
		self._micChannels = None
		self._micSampwidth = 2
		self._micSamplerate = None
		self._sysData = None
		self._sysSamplerate = None
		# Pre-resampled stereo tracks (no gain). Volume changes only apply
		# gain + mix to these, which is far cheaper than resampling again.
		self._micMixBase = None
		self._sysMixBase = None
		self._undoStack = []
		self._redoStack = []
		try:
			self._micGainDb = float(config.conf["cloudUploader"]["micGainDb"])
		except Exception:
			self._micGainDb = 0.0
		try:
			self._sysGainDb = float(config.conf["cloudUploader"]["systemGainDb"])
		except Exception:
			self._sysGainDb = 0.0
		self._volumeApplyTimer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._onVolumeApplyTimer, self._volumeApplyTimer)
		self._pendingVolumeAnnounce = None
		self._mixBusy = False

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		self.statusCtrl = wx.StaticText(self, label=_("Not recording. Press Record to begin."))
		mainSizer.Add(self.statusCtrl, flag=wx.ALL | wx.EXPAND, border=10)

		sourceSizer = wx.BoxSizer(wx.HORIZONTAL)
		sourceLabel = wx.StaticText(self, label=_("Record:"))
		sourceSizer.Add(sourceLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
		self._sourceModeKeys = [key for key, label in RECORD_SOURCE_MODES]
		sourceLabels = [label for key, label in RECORD_SOURCE_MODES]
		self.sourceChoice = wx.Choice(self, choices=sourceLabels)
		try:
			sourceIndex = self._sourceModeKeys.index(config.conf["cloudUploader"]["recordSourceMode"])
		except ValueError:
			sourceIndex = 0
		self.sourceChoice.SetSelection(sourceIndex)
		self.sourceChoice.Bind(wx.EVT_CHOICE, self.onSourceModeChanged)
		self._sourceMode = self._sourceModeKeys[sourceIndex]
		sourceSizer.Add(self.sourceChoice)
		mainSizer.Add(sourceSizer, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		micDeviceSizer = wx.BoxSizer(wx.HORIZONTAL)
		micDeviceLabel = wx.StaticText(self, label=_("Microphone device:"))
		micDeviceSizer.Add(micDeviceLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
		self._micDevices = _listInputDevices()
		micDeviceLabels = [name for devId, name in self._micDevices]
		self.micDeviceChoiceCtrl = wx.Choice(self, choices=micDeviceLabels)
		configuredMicId = config.conf["cloudUploader"]["micDeviceId"]
		micDeviceIndex = 0
		for i, (devId, devName) in enumerate(self._micDevices):
			if devId == configuredMicId:
				micDeviceIndex = i
				break
		self.micDeviceChoiceCtrl.SetSelection(micDeviceIndex)
		micDeviceSizer.Add(self.micDeviceChoiceCtrl)
		mainSizer.Add(micDeviceSizer, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		systemDeviceSizer = wx.BoxSizer(wx.HORIZONTAL)
		systemDeviceLabel = wx.StaticText(self, label=_("Computer audio comes from:"))
		systemDeviceSizer.Add(systemDeviceLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
		self._systemDevices = _listOutputDevices()
		systemDeviceLabels = [name for devId, name in self._systemDevices]
		self.systemDeviceChoiceCtrl = wx.Choice(self, choices=systemDeviceLabels)
		configuredSystemId = config.conf["cloudUploader"]["systemDeviceId"] or None
		systemDeviceIndex = 0
		for i, (devId, devName) in enumerate(self._systemDevices):
			if devId == configuredSystemId:
				systemDeviceIndex = i
				break
		self.systemDeviceChoiceCtrl.SetSelection(systemDeviceIndex)
		systemDeviceSizer.Add(self.systemDeviceChoiceCtrl)
		mainSizer.Add(systemDeviceSizer, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		self.separateTracksCheckbox = wx.CheckBox(
			self, label=_("Save microphone and computer audio as separate tracks")
		)
		self.separateTracksCheckbox.SetValue(config.conf["cloudUploader"]["saveSeparateTracks"])
		mainSizer.Add(self.separateTracksCheckbox, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		self.saveDefaultsBtn = wx.Button(self, label=_("Save these settings for future recordings"))
		self.saveDefaultsBtn.Bind(wx.EVT_BUTTON, self.onSaveDefaults)
		mainSizer.Add(self.saveDefaultsBtn, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		# Created (and therefore reached by Tab) right after the setup
		# controls above, and before Record - not stranded in between
		# Record and whichever real action button happens to already be
		# enabled, which is where it ends up if it's created later: those
		# are all disabled until a recording exists, and Tab skips
		# disabled controls, so Cancel would be the very next stop after
		# Record regardless of where in the layout it's placed.
		cancelBtn = wx.Button(self, id=wx.ID_CANCEL, label=_("Cancel"))
		cancelBtn.Bind(wx.EVT_BUTTON, self.onCancel)
		mainSizer.Add(cancelBtn, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER, border=10)

		self.recordBtn = wx.Button(self, label=_("&Record"))
		self.recordBtn.Bind(wx.EVT_BUTTON, self.onRecordToggle)
		mainSizer.Add(self.recordBtn, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		self.previewBtn = wx.Button(self, label=_("&Preview"))
		self.previewBtn.Bind(wx.EVT_BUTTON, self.onPreviewToggle)
		self.previewBtn.Disable()
		mainSizer.Add(self.previewBtn, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		manageSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.removeBtn = wx.Button(self, label=_("Re&move recording"))
		self.removeBtn.Bind(wx.EVT_BUTTON, self.onRemoveRecording)
		self.removeBtn.Disable()
		manageSizer.Add(self.removeBtn, flag=wx.RIGHT, border=10)
		self.renameBtn = wx.Button(self, label=_("R&ename recording"))
		self.renameBtn.Bind(wx.EVT_BUTTON, self.onRenameRecording)
		self.renameBtn.Disable()
		manageSizer.Add(self.renameBtn)
		mainSizer.Add(manageSizer, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER, border=10)

		silenceSizer = wx.BoxSizer(wx.HORIZONTAL)
		silenceLabel = wx.StaticText(self, label=_("Silence removal (microphone only):"))
		silenceSizer.Add(silenceLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
		self._sensitivityKeys = ["light", "medium", "aggressive"]
		sensitivityLabels = [
			_("Light"),
			_("Medium"),
			_("Aggressive"),
		]
		self.sensitivityChoice = wx.Choice(self, choices=sensitivityLabels)
		try:
			sensitivityIndex = self._sensitivityKeys.index(config.conf["cloudUploader"]["silenceSensitivity"])
		except ValueError:
			sensitivityIndex = 1
		self.sensitivityChoice.SetSelection(sensitivityIndex)
		self.sensitivityChoice.Bind(wx.EVT_CHOICE, self.onSensitivityChanged)
		self.sensitivityChoice.Disable()
		silenceSizer.Add(self.sensitivityChoice, flag=wx.RIGHT, border=10)
		self.silenceBtn = wx.Button(self, label=_("Remove si&lence"))
		self.silenceBtn.Bind(wx.EVT_BUTTON, self.onRemoveSilence)
		self.silenceBtn.Disable()
		silenceSizer.Add(self.silenceBtn)
		mainSizer.Add(silenceSizer, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER, border=10)

		noiseSizer = wx.BoxSizer(wx.HORIZONTAL)
		noiseLabel = wx.StaticText(self, label=_("Noise reduction sensitivity (microphone only):"))
		noiseSizer.Add(noiseLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
		self._noiseSensitivityKeys = ["light", "medium", "aggressive"]
		noiseSensitivityLabels = [
			_("Light"),
			_("Medium"),
			_("Aggressive"),
		]
		self.noiseSensitivityChoice = wx.Choice(self, choices=noiseSensitivityLabels)
		try:
			noiseSensitivityIndex = self._noiseSensitivityKeys.index(
				config.conf["cloudUploader"]["noiseReductionSensitivity"]
			)
		except ValueError:
			noiseSensitivityIndex = 1
		self.noiseSensitivityChoice.SetSelection(noiseSensitivityIndex)
		self.noiseSensitivityChoice.Bind(wx.EVT_CHOICE, self.onNoiseSensitivityChanged)
		self.noiseSensitivityChoice.Disable()
		noiseSizer.Add(self.noiseSensitivityChoice, flag=wx.RIGHT, border=10)
		self.noiseBtn = wx.Button(self, label=_("Re&duce noise"))
		self.noiseBtn.Bind(wx.EVT_BUTTON, self.onReduceNoise)
		self.noiseBtn.Disable()
		noiseSizer.Add(self.noiseBtn)
		mainSizer.Add(noiseSizer, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER, border=10)

		gainSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.normalizeBtn = wx.Button(self, label=_("&Normalize volume"))
		self.normalizeBtn.Bind(wx.EVT_BUTTON, self.onNormalize)
		self.normalizeBtn.Disable()
		gainSizer.Add(self.normalizeBtn, flag=wx.ALL, border=5)
		self.undoBtn = wx.Button(self, label=_("Undo (Ctrl+Z)"))
		self.undoBtn.Bind(wx.EVT_BUTTON, self.onUndo)
		self.undoBtn.Disable()
		gainSizer.Add(self.undoBtn, flag=wx.ALL, border=5)
		self.redoBtn = wx.Button(self, label=_("Redo (Ctrl+Y)"))
		self.redoBtn.Bind(wx.EVT_BUTTON, self.onRedo)
		self.redoBtn.Disable()
		gainSizer.Add(self.redoBtn, flag=wx.ALL, border=5)
		mainSizer.Add(gainSizer, flag=wx.ALIGN_CENTER)

		balanceSizer = wx.BoxSizer(wx.VERTICAL)
		micVolSizer = wx.BoxSizer(wx.HORIZONTAL)
		micVolLabel = wx.StaticText(self, label=_("Microphone volume:"))
		micVolSizer.Add(micVolLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
		self.micVolumeSlider = wx.Slider(
			self, minValue=-20, maxValue=20, value=int(round(self._micGainDb)), style=wx.SL_HORIZONTAL
		)
		# Only apply the expensive mix rewrite when the user finishes moving
		# the slider (or after a short idle), not on every intermediate tick.
		self.micVolumeSlider.Bind(wx.EVT_SLIDER, self.onMicVolumeDragging)
		self.micVolumeSlider.Bind(wx.EVT_SCROLL_CHANGED, self.onMicVolumeChanged)
		micVolSizer.Add(self.micVolumeSlider, flag=wx.EXPAND, proportion=1)
		balanceSizer.Add(micVolSizer, flag=wx.EXPAND | wx.BOTTOM, border=5)
		sysVolSizer = wx.BoxSizer(wx.HORIZONTAL)
		sysVolLabel = wx.StaticText(self, label=_("Computer audio volume:"))
		sysVolSizer.Add(sysVolLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=5)
		self.systemVolumeSlider = wx.Slider(
			self, minValue=-20, maxValue=20, value=int(round(self._sysGainDb)), style=wx.SL_HORIZONTAL
		)
		self.systemVolumeSlider.Bind(wx.EVT_SLIDER, self.onSystemVolumeDragging)
		self.systemVolumeSlider.Bind(wx.EVT_SCROLL_CHANGED, self.onSystemVolumeChanged)
		sysVolSizer.Add(self.systemVolumeSlider, flag=wx.EXPAND, proportion=1)
		balanceSizer.Add(sysVolSizer, flag=wx.EXPAND)
		mainSizer.Add(balanceSizer, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		actionBtnSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.uploadSaveBtn = wx.Button(self, label=_("Upload and Sa&ve"))
		self.uploadSaveBtn.Disable()
		self.uploadSaveBtn.Bind(wx.EVT_BUTTON, self.onUploadAndSave)
		actionBtnSizer.Add(self.uploadSaveBtn, flag=wx.RIGHT, border=10)
		self.uploadNoSaveBtn = wx.Button(self, label=_("Upload &Without Saving"))
		self.uploadNoSaveBtn.Disable()
		self.uploadNoSaveBtn.Bind(wx.EVT_BUTTON, self.onUploadWithoutSaving)
		actionBtnSizer.Add(self.uploadNoSaveBtn, flag=wx.RIGHT, border=10)
		self.keepBtn = wx.Button(self, label=_("&Keep Without Uploading"))
		self.keepBtn.Disable()
		self.keepBtn.Bind(wx.EVT_BUTTON, self.onKeepWithoutUploading)
		actionBtnSizer.Add(self.keepBtn)
		mainSizer.Add(actionBtnSizer, flag=wx.ALL | wx.ALIGN_CENTER, border=10)

		self.SetSizerAndFit(mainSizer)
		self.SetSize((420, self.GetSize().GetHeight()))
		self.SetEscapeId(wx.ID_CANCEL)
		self.Bind(wx.EVT_CHAR_HOOK, self._onCharHook)
		self.Bind(wx.EVT_CLOSE, self.onCancel)

		idUndo = wx.NewIdRef()
		idRedo = wx.NewIdRef()
		self.Bind(wx.EVT_MENU, self.onUndo, id=idUndo)
		self.Bind(wx.EVT_MENU, self.onRedo, id=idRedo)
		self.SetAcceleratorTable(wx.AcceleratorTable([
			(wx.ACCEL_CTRL, ord("Z"), idUndo),
			(wx.ACCEL_CTRL, ord("Y"), idRedo),
		]))

		self._timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._onTimer, self._timer)
		self._previewTimer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._onPreviewTimer, self._previewTimer)

		self._updateSourceControls()
		self.recordBtn.SetFocus()
		# Start capture on the next event-loop turn so the dialog is fully
		# shown and focused before devices open and "Now recording" is spoken.
		if self._autoStart:
			wx.CallAfter(self._startRecording)
		elif self._preload is not None:
			# Background (NVDA+shift+c) recording already captured; load it
			# once the dialog is on screen so controls enable correctly.
			wx.CallAfter(self._applyPreload, self._preload)

	def _applyPreload(self, preload):
		"""Applies a recording that was captured outside this dialog (e.g.
		via the headless NVDA+shift+c toggle) so the usual post-record
		controls are available immediately."""
		try:
			sourceMode = preload.get("sourceMode") or "mic"
			if sourceMode in self._sourceModeKeys:
				self.sourceChoice.SetSelection(self._sourceModeKeys.index(sourceMode))
			self._sourceMode = sourceMode
			self.separateTracksCheckbox.SetValue(bool(preload.get("separate")))
			if preload.get("outPath"):
				self._outputPath = preload["outPath"]
			if preload.get("micPath"):
				self._micOutputPath = preload["micPath"]
			if preload.get("sysPath"):
				self._sysOutputPath = preload["sysPath"]
			self._stopProcessDone(
				preload.get("micData"),
				preload.get("micChannels"),
				preload.get("micSampwidth") or 2,
				preload.get("micRate"),
				preload.get("sysData"),
				preload.get("sysRate"),
				preload.get("mixBaseMic"),
				preload.get("mixBaseSys"),
				preload.get("current"),
				preload.get("channels") or 2,
				preload.get("mixRate") or 44100,
			)
		except Exception as e:
			log.error("Cloud Uploader: could not load background recording: %s" % e)
			gui.messageBox(
				_("Could not load the recording: {error}").format(error=e),
				_("Recording error"),
				wx.OK | wx.ICON_ERROR,
				self,
			)

	def _onCharHook(self, evt):
		if evt.GetKeyCode() == wx.WXK_ESCAPE:
			self.onCancel(evt)
			return
		evt.Skip()

	def _updateSourceControls(self):
		# Once a recording has been made in this dialog, keep the source
		# and device pickers locked. Changing them after the fact would
		# leave the already-captured data mismatched with the UI, and the
		# user asked not to see those controls as editable after recording.
		locked = self._recording or self._hasRecording
		key = self._sourceModeKeys[self.sourceChoice.GetSelection()]
		self.sourceChoice.Enable(not locked)
		self.micDeviceChoiceCtrl.Enable(key in ("mic", "both") and not locked)
		self.systemDeviceChoiceCtrl.Enable(key in ("computer", "both") and not locked)
		self.separateTracksCheckbox.Enable(key == "both" and not locked)
		# Volume balance applies when recording both; editable before and
		# after capture so defaults can be set, and the live mix adjusted.
		volEnabled = key == "both" and not self._recording
		try:
			self.micVolumeSlider.Enable(volEnabled)
			self.systemVolumeSlider.Enable(volEnabled)
			self.saveDefaultsBtn.Enable(not locked)
		except RuntimeError:
			pass

	def onSourceModeChanged(self, evt):
		self._updateSourceControls()

	def onSaveDefaults(self, evt):
		"""Writes the current source/device/separate-tracks/volume choices to
		config so the next recording dialog opens with the same values,
		without needing to start a recording first."""
		sourceKey = self._sourceModeKeys[self.sourceChoice.GetSelection()]
		config.conf["cloudUploader"]["recordSourceMode"] = sourceKey
		config.conf["cloudUploader"]["micDeviceId"] = self._micDevices[self.micDeviceChoiceCtrl.GetSelection()][0]
		systemDeviceId = self._systemDevices[self.systemDeviceChoiceCtrl.GetSelection()][0]
		config.conf["cloudUploader"]["systemDeviceId"] = systemDeviceId or ""
		config.conf["cloudUploader"]["saveSeparateTracks"] = self.separateTracksCheckbox.GetValue()
		config.conf["cloudUploader"]["micGainDb"] = float(self.micVolumeSlider.GetValue())
		config.conf["cloudUploader"]["systemGainDb"] = float(self.systemVolumeSlider.GetValue())
		self._micGainDb = float(self.micVolumeSlider.GetValue())
		self._sysGainDb = float(self.systemVolumeSlider.GetValue())
		ui.message(_("Settings saved for future recordings"))

	def onRecordToggle(self, evt):
		if self._recording:
			self._stopRecording()
		else:
			self._startRecording()

	def _startRecording(self):
		self._stopPreview()
		sourceKey = self._sourceModeKeys[self.sourceChoice.GetSelection()]
		config.conf["cloudUploader"]["recordSourceMode"] = sourceKey
		deviceId = self._micDevices[self.micDeviceChoiceCtrl.GetSelection()][0]
		config.conf["cloudUploader"]["micDeviceId"] = deviceId
		systemDeviceId = self._systemDevices[self.systemDeviceChoiceCtrl.GetSelection()][0]
		config.conf["cloudUploader"]["systemDeviceId"] = systemDeviceId or ""
		config.conf["cloudUploader"]["saveSeparateTracks"] = self.separateTracksCheckbox.GetValue()
		preferMono = config.conf["cloudUploader"]["micPreferMono"]

		micRecorder = None
		sysRecorder = None
		usedFallbackMicDevice = False
		usedFallbackSystemDevice = False
		# Open (prepare) both devices first, then start capture on both as
		# close together as possible so dual-source tracks stay in sync.
		try:
			if sourceKey in ("mic", "both"):
				micRecorder = _WaveInRecorder(deviceId=deviceId, preferMono=preferMono)
				micRecorder.open()
				usedFallbackMicDevice = micRecorder.usedFallbackDevice
			if sourceKey in ("computer", "both"):
				sysRecorder = _LoopbackRecorder(deviceId=systemDeviceId)
				sysRecorder.open()
				usedFallbackSystemDevice = sysRecorder.usedFallbackDevice
		except Exception as e:
			log.error("Cloud Uploader: could not open recording device: %s" % e)
			for rec in (micRecorder, sysRecorder):
				if rec is not None:
					try:
						rec.abort()
					except Exception:
						pass
			gui.messageBox(
				_("Could not start recording: {error}").format(error=e),
				_("Recording error"),
				wx.OK | wx.ICON_ERROR,
				self,
			)
			return
		sysStartClock = None
		micStartClock = None
		startErrors = []
		try:
			if sysRecorder is not None and micRecorder is not None:
				# Calling startCapture() one after another means the second
				# device isn't even told to start until the first device's
				# blocking Win32/WASAPI start call has fully finished
				# internally, which can be several ms on its own. Releasing
				# both from a shared barrier lets the two blocking calls
				# actually overlap - each one runs on its own thread and
				# releases the GIL while it blocks - so the real hardware
				# start times land much closer together than calling them
				# sequentially ever could.
				barrier = threading.Barrier(2)

				def _startSys():
					try:
						barrier.wait()
						sysRecorder.startCapture()
					except Exception as e:
						startErrors.append(e)
					finally:
						nonlocal sysStartClock
						sysStartClock = time.perf_counter()

				def _startMic():
					try:
						barrier.wait()
						micRecorder.startCapture()
					except Exception as e:
						startErrors.append(e)
					finally:
						nonlocal micStartClock
						micStartClock = time.perf_counter()

				sysThread = threading.Thread(target=_startSys)
				micThread = threading.Thread(target=_startMic)
				sysThread.start()
				micThread.start()
				sysThread.join()
				micThread.join()
				if startErrors:
					raise startErrors[0]
			elif sysRecorder is not None:
				sysRecorder.startCapture()
				sysStartClock = time.perf_counter()
			elif micRecorder is not None:
				micRecorder.startCapture()
				micStartClock = time.perf_counter()
		except Exception as e:
			log.error("Cloud Uploader: could not start capture: %s" % e)
			for rec in (micRecorder, sysRecorder):
				if rec is not None:
					try:
						rec.abort()
					except Exception:
						pass
			gui.messageBox(
				_("Could not start recording: {error}").format(error=e),
				_("Recording error"),
				wx.OK | wx.ICON_ERROR,
				self,
			)
			return

		self._micRecorder = micRecorder
		self._sysRecorder = sysRecorder
		# Measured directly from when each device's capture actually began,
		# rather than inferred later from recorded length - that inference
		# also picks up drift from stop() being called on the two recorders
		# one after another, not just from the start-time gap.
		if micStartClock is not None and sysStartClock is not None:
			self._startClockOffset = micStartClock - sysStartClock
		else:
			self._startClockOffset = 0.0
		self._sourceMode = sourceKey
		self._recording = True
		self._hasRecording = False
		self._undoStack = []
		self._redoStack = []
		self._micGainDb = float(self.micVolumeSlider.GetValue())
		self._sysGainDb = float(self.systemVolumeSlider.GetValue())
		self._volumeDirty = False
		self._startTime = time.time()
		self.recordBtn.SetLabel(_("&Stop"))
		self.sourceChoice.Disable()
		self.micDeviceChoiceCtrl.Disable()
		self.systemDeviceChoiceCtrl.Disable()
		self.separateTracksCheckbox.Disable()
		self.saveDefaultsBtn.Disable()
		self.previewBtn.Disable()
		self.normalizeBtn.Disable()
		self.sensitivityChoice.Disable()
		self.silenceBtn.Disable()
		self.noiseSensitivityChoice.Disable()
		self.noiseBtn.Disable()
		self.undoBtn.Disable()
		self.redoBtn.Disable()
		self.micVolumeSlider.Disable()
		self.systemVolumeSlider.Disable()
		self.uploadSaveBtn.Disable()
		self.uploadNoSaveBtn.Disable()
		self.keepBtn.Disable()
		self.removeBtn.Disable()
		self.renameBtn.Disable()
		self.statusCtrl.SetLabel(_("Recording... 0:00"))
		self.recordBtn.SetFocus()
		if usedFallbackMicDevice and usedFallbackSystemDevice:
			ui.message(_("The selected microphone and computer audio device weren't available, using the system defaults instead"))
		elif usedFallbackMicDevice:
			ui.message(_("The selected microphone wasn't available, using the system default instead"))
		elif usedFallbackSystemDevice:
			ui.message(_("The selected computer audio device wasn't available, using the system default instead"))
		else:
			ui.message(_("Now recording"))
		self._timer.Start(50)

	def _stopRecording(self):
		self._timer.Stop()
		self.recordBtn.Disable()
		self.statusCtrl.SetLabel(_("Processing recording, please wait..."))
		ui.message(_("Processing recording, please wait..."))
		try:
			micRaw = self._micRecorder.stop() if self._micRecorder else None
			sysRaw = self._sysRecorder.stop() if self._sysRecorder else None
			micChannels = self._micRecorder.channels if self._micRecorder else None
			micRate = self._micRecorder.samplerate if self._micRecorder else None
			micSampwidth = (self._micRecorder.bitspersample // 8) if self._micRecorder else 2
			sysRate = self._sysRecorder.samplerate if self._sysRecorder else None
		except Exception as e:
			log.error("Cloud Uploader: could not stop recording: %s" % e)
			self._recording = False
			self.recordBtn.Enable()
			self.recordBtn.SetLabel(_("&Record"))
			self.statusCtrl.SetLabel(_("Not recording. Press Record to begin."))
			gui.messageBox(
				_("Could not save the recording: {error}").format(error=e),
				_("Recording error"),
				wx.OK | wx.ICON_ERROR,
				self,
			)
			return
		self._recording = False
		self._micRecorder = None
		self._sysRecorder = None
		# Alignment, mix and disk write all run off the UI thread so Stop
		# returns immediately after the devices are closed.
		args = (
			micRaw, micChannels, micSampwidth, micRate,
			sysRaw, sysRate,
			self._sourceMode, self.separateTracksCheckbox.GetValue(),
			self._outputPath, self._micOutputPath, self._sysOutputPath,
			self._startClockOffset,
		)
		threading.Thread(target=self._stopProcessThread, args=args, daemon=True).start()

	def _stopProcessThread(
		self, micRaw, micChannels, micSampwidth, micRate,
		sysRaw, sysRate, sourceMode, separate, outPath, micPath, sysPath,
		startClockOffset,
	):
		try:
			result = _processCapturedAudio(
				micRaw, micChannels, micSampwidth, micRate,
				sysRaw, sysRate, sourceMode, separate,
				outPath, micPath, sysPath, startClockOffset,
			)
		except Exception as e:
			log.error("Cloud Uploader: could not process recording: %s" % e)
			wx.CallAfter(self._stopProcessFailed, str(e))
			return
		wx.CallAfter(
			self._stopProcessDone,
			result["micData"], result["micChannels"], result["micSampwidth"], result["micRate"],
			result["sysData"], result["sysRate"],
			result["mixBaseMic"], result["mixBaseSys"], result["current"],
			result["channels"], result["mixRate"],
		)

	def _stopProcessFailed(self, errorDetail):
		self.recordBtn.Enable()
		self.recordBtn.SetLabel(_("&Record"))
		self.statusCtrl.SetLabel(_("Not recording. Press Record to begin."))
		gui.messageBox(
			_("Could not save the recording: {error}").format(error=errorDetail),
			_("Recording error"),
			wx.OK | wx.ICON_ERROR,
			self,
		)

	def _stopProcessDone(
		self, micData, micChannels, micSampwidth, micRate,
		sysData, sysRate, mixBaseMic, mixBaseSys, current, channels, mixRate,
	):
		self._micData = micData
		self._micChannels = micChannels
		self._micSampwidth = micSampwidth
		self._micSamplerate = micRate
		self._sysData = sysData
		self._sysSamplerate = sysRate
		self._micMixBase = mixBaseMic
		self._sysMixBase = mixBaseSys
		self._currentData = current
		self._currentChannels = channels
		self._mixSampleRate = mixRate
		self._volumeDirty = abs(self._micGainDb) > 0.001 or abs(self._sysGainDb) > 0.001
		self._hasRecording = True
		self.recordBtn.Enable()
		self.recordBtn.SetLabel(_("&Record again"))
		self._updateSourceControls()
		self.previewBtn.Enable()
		self.removeBtn.Enable()
		self.renameBtn.Enable()
		self.uploadSaveBtn.Enable()
		self.uploadNoSaveBtn.Enable()
		self.keepBtn.Enable()
		hasMic = self._micData is not None
		hasSys = self._sysData is not None
		hasBoth = hasMic and hasSys
		self.normalizeBtn.Enable(hasMic or hasSys)
		self.sensitivityChoice.Enable(hasMic)
		self.silenceBtn.Enable(hasMic)
		self.noiseSensitivityChoice.Enable(hasMic)
		self.noiseBtn.Enable(hasMic)
		self.micVolumeSlider.Enable(hasBoth)
		self.systemVolumeSlider.Enable(hasBoth)
		clipSeconds = self._clipSeconds()
		minutes, seconds = divmod(int(clipSeconds), 60)
		self.statusCtrl.SetLabel(
			_("Recording complete, length {m}:{s:02d}").format(m=minutes, s=seconds)
		)
		ui.message(_("Recording stopped"))
		self.previewBtn.SetFocus()

	def _clipSeconds(self):
		if not self._currentData or not self._mixSampleRate:
			return 0.0
		return (len(self._currentData) // (self._currentChannels * 2)) / float(self._mixSampleRate)

	def _invalidateMixBases(self):
		self._micMixBase = None
		self._sysMixBase = None

	def _recomputeCurrentData(self):
		"""Rebuilds self._currentData from mic/sys tracks + balance gains.
		Uses cached pre-resampled bases when available so volume changes
		do not repeat expensive resampling."""
		mixBaseMic, mixBaseSys, current, channels, mixRate = _buildMix(
			self._micData, self._micChannels, self._micSamplerate,
			self._sysData, self._sysSamplerate,
			self._micGainDb, self._sysGainDb,
			micBase=self._micMixBase, sysBase=self._sysMixBase,
		)
		self._micMixBase = mixBaseMic
		self._sysMixBase = mixBaseSys
		self._currentData = current
		self._currentChannels = channels
		self._mixSampleRate = mixRate

	def _writeAllOutputs(self):
		self._writeMixedFile()
		if self._sourceMode == "both" and self.separateTracksCheckbox.GetValue():
			self._writeMicFile()
			self._writeSysFile()

	def _writeMixedFile(self):
		_removeIfExists(self._outputPath)
		_writeWavRaw(self._outputPath, self._currentData, self._currentChannels, 2, self._mixSampleRate)

	def _writeMicFile(self):
		if self._micData is None:
			return
		_removeIfExists(self._micOutputPath)
		_writeWavRaw(self._micOutputPath, self._micData, self._micChannels, self._micSampwidth, self._micSamplerate)

	def _writeSysFile(self):
		if self._sysData is None:
			return
		_removeIfExists(self._sysOutputPath)
		_writeWavRaw(self._sysOutputPath, self._sysData, 2, 2, self._sysSamplerate)

	def onSensitivityChanged(self, evt):
		key = self._sensitivityKeys[self.sensitivityChoice.GetSelection()]
		config.conf["cloudUploader"]["silenceSensitivity"] = key

	def onRemoveSilence(self, evt):
		if self._micData is None:
			return
		key = self._sensitivityKeys[self.sensitivityChoice.GetSelection()]
		preset = SILENCE_PRESETS.get(key, SILENCE_PRESETS["medium"])
		wasPlaying, posMs = self._pausePreviewForEdit()
		bothTracks = self._sysData is not None
		if bothTracks:
			# Never trim when a computer-audio track was recorded alongside
			# the microphone - cutting the mic would pull it out of sync.
			processed, affectedSeconds = _muteSilence(
				self._micData, self._micChannels, self._micSampwidth, self._micSamplerate, **preset
			)
		else:
			processed, affectedSeconds = _removeSilence(
				self._micData, self._micChannels, self._micSampwidth, self._micSamplerate, **preset
			)
		if affectedSeconds < 0.1:
			ui.message(_("No silence found to remove at this sensitivity"))
			self._resumePreviewIfNeeded(wasPlaying, posMs)
			return
		self._pushUndo()
		self._micData = processed
		self._invalidateMixBases()
		self._recomputeCurrentData()
		self._writeAllOutputs()
		clipSeconds = self._clipSeconds()
		minutes, seconds = divmod(int(clipSeconds), 60)
		if bothTracks:
			self.statusCtrl.SetLabel(
				_("Muted {seconds:.1f} seconds of silence in the microphone track. Length {m}:{s:02d}").format(
					seconds=affectedSeconds, m=minutes, s=seconds
				)
			)
			ui.message(_("Muted {seconds:.1f} seconds of silence in the microphone track").format(seconds=affectedSeconds))
		else:
			self.statusCtrl.SetLabel(
				_("Removed {removed:.1f} seconds of silence. Length {m}:{s:02d}").format(
					removed=affectedSeconds, m=minutes, s=seconds
				)
			)
			ui.message(_("Removed {removed:.1f} seconds of silence").format(removed=affectedSeconds))
		self.silenceBtn.SetFocus()
		self._resumePreviewIfNeeded(wasPlaying, posMs)

	def onNoiseSensitivityChanged(self, evt):
		key = self._noiseSensitivityKeys[self.noiseSensitivityChoice.GetSelection()]
		config.conf["cloudUploader"]["noiseReductionSensitivity"] = key

	def onReduceNoise(self, evt):
		self._runNoiseReduction(auto=False)

	def _runNoiseReduction(self, auto=False):
		if self._micData is None:
			return
		wasPlaying, posMs = self._pausePreviewForEdit()
		key = self._noiseSensitivityKeys[self.noiseSensitivityChoice.GetSelection()]
		self._pushUndo()
		self._setEditingControlsEnabled(False)
		dataSnapshot = bytearray(self._micData)
		channels, sampwidth, samplerate = self._micChannels, self._micSampwidth, self._micSamplerate
		ffmpegPath = _findFfmpeg()
		if ffmpegPath:
			if not auto:
				self.statusCtrl.SetLabel(_("Reducing noise..."))
				ui.message(_("Reducing noise..."))
			thread = threading.Thread(
				target=self._reduceNoiseFfmpegThread,
				args=(ffmpegPath, dataSnapshot, channels, sampwidth, samplerate, key, wasPlaying, posMs),
				daemon=True,
			)
		else:
			# No ffmpeg: fall back to a pure-Python spectral subtraction.
			# It works, but running an FFT per analysis window in
			# interpreted Python is far slower than ffmpeg's compiled
			# afftdn filter, so this can take noticeably longer - said
			# up front rather than leaving the wait unexplained.
			preset = NOISE_REDUCTION_PRESETS.get(key, NOISE_REDUCTION_PRESETS["medium"])
			if not auto:
				self.statusCtrl.SetLabel(_("Reducing noise, please wait (slower without ffmpeg)..."))
				ui.message(_("Reducing noise, please wait, this may take a while without ffmpeg installed..."))
			thread = threading.Thread(
				target=self._reduceNoisePurePythonThread,
				args=(dataSnapshot, channels, sampwidth, samplerate, preset, wasPlaying, posMs),
				daemon=True,
			)
		thread.start()

	def _reduceNoiseFfmpegThread(self, ffmpegPath, dataSnapshot, channels, sampwidth, samplerate, key, wasPlaying, posMs):
		tmpDir = tempfile.mkdtemp(prefix="cuNoiseReduce")
		try:
			inPath = os.path.join(tmpDir, "in.wav")
			outPath = os.path.join(tmpDir, "out.wav")
			_writeWavRaw(inPath, dataSnapshot, channels, sampwidth, samplerate)
			nrLevel = NOISE_REDUCTION_NR_LEVELS.get(key, NOISE_REDUCTION_NR_LEVELS["medium"])
			margin = NOISE_REDUCTION_NF_MARGIN_DB.get(key, 0.0)
			floorDb = _estimateNoiseFloorDb(dataSnapshot, channels, sampwidth, samplerate)
			_denoiseWithFfmpeg(ffmpegPath, inPath, outPath, nrLevel, floorDb + margin)
			processed, outChannels, outSampwidth, outFramerate = _readWavRaw(outPath)
			if outChannels != channels or outSampwidth != sampwidth or outFramerate != samplerate:
				# afftdn shouldn't change the format, but if it ever does,
				# don't silently hand back audio that no longer matches
				# what the rest of the dialog thinks it's working with.
				raise Exception("ffmpeg changed the audio format unexpectedly")
			reductionDb = _estimateNoiseReductionDb(dataSnapshot, processed, channels, sampwidth, samplerate)
		except Exception as e:
			log.error("Cloud Uploader: ffmpeg noise reduction failed, falling back to pure Python: %s" % e)
			shutil.rmtree(tmpDir, ignore_errors=True)
			self._reduceNoisePurePythonThread(
				dataSnapshot, channels, sampwidth, samplerate,
				NOISE_REDUCTION_PRESETS.get(key, NOISE_REDUCTION_PRESETS["medium"]), wasPlaying, posMs,
			)
			return
		shutil.rmtree(tmpDir, ignore_errors=True)
		wx.CallAfter(self._noiseReductionDone, processed, reductionDb, wasPlaying, posMs)

	def _reduceNoisePurePythonThread(self, dataSnapshot, channels, sampwidth, samplerate, preset, wasPlaying, posMs):
		try:
			processed, reductionDb = _reduceNoise(dataSnapshot, channels, sampwidth, samplerate, **preset)
		except Exception as e:
			log.error("Cloud Uploader: noise reduction failed: %s" % e)
			wx.CallAfter(self._noiseReductionFailed, str(e), wasPlaying, posMs)
			return
		wx.CallAfter(self._noiseReductionDone, processed, reductionDb, wasPlaying, posMs)

	def _noiseReductionDone(self, processed, reductionDb, wasPlaying, posMs):
		self._micData = processed
		self._invalidateMixBases()
		self._recomputeCurrentData()
		self._writeAllOutputs()
		self._setEditingControlsEnabled(True)
		clipSeconds = self._clipSeconds()
		minutes, seconds = divmod(int(clipSeconds), 60)
		# No separate "no significant background noise" warning - noise
		# reduction is just applied, silently, whenever it doesn't find
		# much to do.
		self.statusCtrl.SetLabel(
			_("Noise reduction applied. Length {m}:{s:02d}").format(m=minutes, s=seconds)
		)
		if reductionDb >= 0.5:
			ui.message(_("Noise reduced by about {db:.0f} dB").format(db=reductionDb))
		self.noiseBtn.SetFocus()
		self._resumePreviewIfNeeded(wasPlaying, posMs)

	def _noiseReductionFailed(self, errorDetail, wasPlaying, posMs):
		self._setEditingControlsEnabled(True)
		if self._undoStack:
			self._undoStack.pop()
			self._refreshUndoRedoButtons()
		gui.messageBox(
			_("Noise reduction failed: {error}").format(error=errorDetail),
			_("Noise reduction failed"),
			wx.OK | wx.ICON_ERROR,
			self,
		)
		self._resumePreviewIfNeeded(wasPlaying, posMs)

	def _setEditingControlsEnabled(self, enabled):
		"""Enables or disables every control that reads or writes the
		current recording, so noise reduction's background thread can't
		race with recording, previewing, or another edit."""
		for ctrl in (
			self.recordBtn, self.previewBtn, self.removeBtn, self.renameBtn,
			self.sensitivityChoice, self.silenceBtn,
			self.noiseSensitivityChoice, self.noiseBtn,
			self.normalizeBtn, self.undoBtn, self.redoBtn,
			self.uploadSaveBtn, self.uploadNoSaveBtn, self.keepBtn,
			self.micVolumeSlider, self.systemVolumeSlider,
		):
			ctrl.Enable(enabled)
		if enabled:
			self._refreshUndoRedoButtons()
			hasMic = self._micData is not None
			hasSys = self._sysData is not None
			hasBoth = hasMic and hasSys
			self.normalizeBtn.Enable(hasMic or hasSys)
			self.sensitivityChoice.Enable(hasMic)
			self.silenceBtn.Enable(hasMic)
			self.noiseSensitivityChoice.Enable(hasMic)
			self.noiseBtn.Enable(hasMic)
			volOk = (self._sourceModeKeys[self.sourceChoice.GetSelection()] == "both") or hasBoth
			self.micVolumeSlider.Enable(volOk and not self._recording)
			self.systemVolumeSlider.Enable(volOk and not self._recording)

	def _pushUndo(self):
		self._undoStack.append(bytearray(self._micData))
		if len(self._undoStack) > 5:
			self._undoStack.pop(0)
		self._redoStack = []
		self._refreshUndoRedoButtons()

	def _refreshUndoRedoButtons(self):
		try:
			self.undoBtn.Enable(bool(self._undoStack))
			self.redoBtn.Enable(bool(self._redoStack))
		except RuntimeError:
			pass

	def onNormalize(self, evt):
		hasMic = self._micData is not None
		hasSys = self._sysData is not None
		if not hasMic and not hasSys:
			return
		micDb = _normalizeGainDb(self._micData, self._micSampwidth) if hasMic else 0.0
		sysDb = _normalizeGainDb(self._sysData, 2) if hasSys else 0.0
		if micDb <= 0.01 and sysDb <= 0.01:
			ui.message(_("Already at maximum volume without clipping"))
			return
		wasPlaying, posMs = self._pausePreviewForEdit()
		if hasMic:
			self._pushUndo()
			if micDb > 0.01:
				self._micData = _applyGainDb(self._micData, self._micSampwidth, micDb)
		if hasSys and sysDb > 0.01:
			self._sysData = _applyGainDb(self._sysData, 2, sysDb)
		self._invalidateMixBases()
		self._recomputeCurrentData()
		self._writeAllOutputs()
		self._volumeDirty = False
		if hasMic and hasSys:
			ui.message(_("Microphone and computer audio volume normalized"))
		else:
			ui.message(_("Volume normalized"))
		self.normalizeBtn.SetFocus()
		self._resumePreviewIfNeeded(wasPlaying, posMs)

	def onUndo(self, evt):
		if not self._undoStack:
			ui.message(_("Nothing to undo"))
			return
		wasPlaying, posMs = self._pausePreviewForEdit()
		self._redoStack.append(bytearray(self._micData))
		self._micData = self._undoStack.pop()
		self._invalidateMixBases()
		self._recomputeCurrentData()
		self._writeAllOutputs()
		self._refreshUndoRedoButtons()
		clipSeconds = self._clipSeconds()
		minutes, seconds = divmod(int(clipSeconds), 60)
		self.statusCtrl.SetLabel(_("Undone. Length {m}:{s:02d}").format(m=minutes, s=seconds))
		ui.message(_("Undone"))
		self.undoBtn.SetFocus()
		self._resumePreviewIfNeeded(wasPlaying, posMs)

	def onRedo(self, evt):
		if not self._redoStack:
			ui.message(_("Nothing to redo"))
			return
		wasPlaying, posMs = self._pausePreviewForEdit()
		self._undoStack.append(bytearray(self._micData))
		self._micData = self._redoStack.pop()
		self._invalidateMixBases()
		self._recomputeCurrentData()
		self._writeAllOutputs()
		self._refreshUndoRedoButtons()
		clipSeconds = self._clipSeconds()
		minutes, seconds = divmod(int(clipSeconds), 60)
		self.statusCtrl.SetLabel(_("Redone. Length {m}:{s:02d}").format(m=minutes, s=seconds))
		ui.message(_("Redone"))
		self.redoBtn.SetFocus()
		self._resumePreviewIfNeeded(wasPlaying, posMs)

	def onMicVolumeDragging(self, evt):
		self._micGainDb = float(self.micVolumeSlider.GetValue())
		self._volumeDirty = True
		if self._playing:
			self._previewPlayer.setGains(self._micGainDb, self._sysGainDb)
		else:
			self._pendingVolumeAnnounce = ("mic", self._micGainDb)
			self._volumeApplyTimer.Start(300, oneShot=True)
		evt.Skip()

	def onSystemVolumeDragging(self, evt):
		self._sysGainDb = float(self.systemVolumeSlider.GetValue())
		self._volumeDirty = True
		if self._playing:
			self._previewPlayer.setGains(self._micGainDb, self._sysGainDb)
		else:
			self._pendingVolumeAnnounce = ("sys", self._sysGainDb)
			self._volumeApplyTimer.Start(300, oneShot=True)
		evt.Skip()

	def onMicVolumeChanged(self, evt):
		self._micGainDb = float(self.micVolumeSlider.GetValue())
		self._volumeDirty = True
		ui.message(_("Microphone volume {db:+.0f} dB").format(db=self._micGainDb))
		if self._playing:
			self._previewPlayer.setGains(self._micGainDb, self._sysGainDb)
		evt.Skip()

	def onSystemVolumeChanged(self, evt):
		self._sysGainDb = float(self.systemVolumeSlider.GetValue())
		self._volumeDirty = True
		ui.message(_("Computer audio volume {db:+.0f} dB").format(db=self._sysGainDb))
		if self._playing:
			self._previewPlayer.setGains(self._micGainDb, self._sysGainDb)
		evt.Skip()

	def _onVolumeApplyTimer(self, evt):
		pending = self._pendingVolumeAnnounce
		self._pendingVolumeAnnounce = None
		if pending is None:
			return
		which, value = pending
		if which == "mic":
			ui.message(_("Microphone volume {db:+.0f} dB").format(db=value))
		else:
			ui.message(_("Computer audio volume {db:+.0f} dB").format(db=value))

	def _mixWithCurrentGains(self):
		"""Returns mixed PCM using current volume gains without writing disk."""
		if self._micMixBase is not None and self._sysMixBase is not None:
			micGained = _applyGainDb(self._micMixBase, 2, self._micGainDb)
			sysGained = _applyGainDb(self._sysMixBase, 2, self._sysGainDb)
			return _mixStereoTracks(micGained, sysGained), 2, self._mixSampleRate or 48000
		self._recomputeCurrentData()
		return self._currentData, self._currentChannels, self._mixSampleRate

	def _flushVolumeIfDirty(self):
		"""Bakes current per-track volume gains into the permanent export
		file(s). Called only when uploading or keeping the recording."""
		if not self._hasRecording or not getattr(self, "_volumeDirty", False):
			return
		data, channels, rate = self._mixWithCurrentGains()
		self._currentData = data
		self._currentChannels = channels
		self._mixSampleRate = rate
		self._writeAllOutputs()
		self._volumeDirty = False
		_removeIfExists(self._previewPath)

	def onRemoveRecording(self, evt):
		self._stopPreview()
		if not self._hasRecording:
			return
		if gui.messageBox(
			_("Delete this recording from your device? This cannot be undone."),
			_("Remove recording"),
			wx.YES | wx.NO | wx.ICON_WARNING,
			self,
		) != wx.YES:
			return
		for path in (self._outputPath, self._previewPath, self._micOutputPath, self._sysOutputPath):
			try:
				if os.path.exists(path):
					os.remove(path)
			except Exception as e:
				log.error("Cloud Uploader: could not remove recording: %s" % e)
		self._hasRecording = False
		self._micData = None
		self._sysData = None
		self._currentData = None
		self._invalidateMixBases()
		self._undoStack = []
		self._redoStack = []
		try:
			self._micGainDb = float(config.conf["cloudUploader"]["micGainDb"])
		except Exception:
			self._micGainDb = 0.0
		try:
			self._sysGainDb = float(config.conf["cloudUploader"]["systemGainDb"])
		except Exception:
			self._sysGainDb = 0.0
		self.micVolumeSlider.SetValue(int(round(self._micGainDb)))
		self.systemVolumeSlider.SetValue(int(round(self._sysGainDb)))
		self.previewBtn.Disable()
		self.normalizeBtn.Disable()
		self.undoBtn.Disable()
		self.redoBtn.Disable()
		self.sensitivityChoice.Disable()
		self.silenceBtn.Disable()
		self.noiseSensitivityChoice.Disable()
		self.noiseBtn.Disable()
		self.micVolumeSlider.Disable()
		self.systemVolumeSlider.Disable()
		self.uploadSaveBtn.Disable()
		self.uploadNoSaveBtn.Disable()
		self.keepBtn.Disable()
		self.removeBtn.Disable()
		self.renameBtn.Disable()
		self.recordBtn.SetLabel(_("&Record"))
		self._updateSourceControls()  # unlock source/device pickers again
		self.statusCtrl.SetLabel(_("Recording removed. Press Record to begin."))
		ui.message(_("Recording removed"))
		self.recordBtn.SetFocus()

	def onRenameRecording(self, evt):
		if not self._hasRecording:
			return
		self._stopPreview()
		currentName = os.path.splitext(os.path.basename(self._outputPath))[0]
		dlg = wx.TextEntryDialog(self, _("Enter a new name for this recording:"), _("Rename recording"), currentName)
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			newName = dlg.GetValue().strip()
		finally:
			dlg.Destroy()
		if not newName:
			return
		for ch in '<>:"/\\|?*':
			newName = newName.replace(ch, "_")
		renamePlan = []
		for attr, suffix in (("_outputPath", ""), ("_micOutputPath", "_mic"), ("_sysOutputPath", "_system")):
			oldPath = getattr(self, attr)
			ext = os.path.splitext(oldPath)[1]
			newPath = os.path.join(_getRecordingsFolder(), newName + suffix + ext)
			if os.path.normcase(newPath) != os.path.normcase(oldPath) and os.path.exists(newPath):
				ui.message(_("A recording with that name already exists"))
				return
			renamePlan.append((attr, oldPath, newPath))
		try:
			for attr, oldPath, newPath in renamePlan:
				if os.path.exists(oldPath):
					os.rename(oldPath, newPath)
		except Exception as e:
			log.error("Cloud Uploader: could not rename recording: %s" % e)
			gui.messageBox(
				_("Could not rename the recording: {error}").format(error=e),
				_("Rename recording"),
				wx.OK | wx.ICON_ERROR,
				self,
			)
			return
		for attr, oldPath, newPath in renamePlan:
			setattr(self, attr, newPath)
		ui.message(_("Recording renamed to {name}").format(name=newName))
		self.renameBtn.SetFocus()

	def _onTimer(self, evt):
		if self._micRecorder is not None:
			self._micRecorder.poll()
		if self._sysRecorder is not None:
			self._sysRecorder.poll()
		elapsed = int(time.time() - self._startTime)
		minutes, seconds = divmod(elapsed, 60)
		self.statusCtrl.SetLabel(_("Recording... {m}:{s:02d}").format(m=minutes, s=seconds))

	def onPreviewToggle(self, evt):
		if self._playing:
			self._stopPreview()
			return
		if not self._hasRecording:
			return
		try:
			self._startStreamingPreview(startMs=0)
		except Exception as e:
			log.error("Cloud Uploader: could not play preview: %s" % e)
			ui.message(_("Could not play preview"))
			return
		self.previewBtn.SetLabel(_("&Stop preview"))
		ui.message(_("Playing preview"))

	def _startStreamingPreview(self, startMs=0):
		single = None
		singleChannels = 2
		micBase = self._micMixBase
		sysBase = self._sysMixBase
		if micBase is None or sysBase is None:
			# Single-source (or bases not ready): play the mixed PCM.
			single = self._currentData
			singleChannels = self._currentChannels or 2
			micBase = None
			sysBase = None
		self._previewPlayer.start(
			micBase, sysBase, single,
			self._mixSampleRate or 44100,
			self._micGainDb, self._sysGainDb,
			startMs=startMs,
			singleChannels=singleChannels,
		)
		self._playing = True
		self._previewLengthMs = self._previewPlayer.lengthMs
		self._previewTimer.Start(30)

	def _onPreviewTimer(self, evt):
		if not self._playing:
			return
		self._previewPlayer.poll()
		if not self._previewPlayer.isPlaying:
			self._stopPreview()

	def _stopPreview(self):
		if not self._playing and not self._previewPlayer.isPlaying:
			return
		self._previewTimer.Stop()
		try:
			self._previewPlayer.stop()
		except Exception:
			pass
		self._playing = False
		try:
			self.previewBtn.SetLabel(_("&Preview"))
		except RuntimeError:
			pass

	def _pausePreviewForEdit(self):
		if not self._playing:
			return False, 0
		posMs = self._previewPlayer.positionMs
		self._stopPreview()
		return True, posMs

	def _resumePreviewIfNeeded(self, wasPlaying, posMs):
		if not wasPlaying:
			return
		if not self._hasRecording:
			return
		try:
			self._startStreamingPreview(startMs=posMs)
			self.previewBtn.SetLabel(_("&Stop preview"))
		except Exception as e:
			log.error("Cloud Uploader: could not resume preview: %s" % e)
			try:
				self.previewBtn.SetLabel(_("&Preview"))
			except RuntimeError:
				pass

	def onUploadAndSave(self, evt):
		self._finish(RESULT_UPLOAD_SAVE)

	def onUploadWithoutSaving(self, evt):
		self._finish(RESULT_UPLOAD_NO_SAVE)

	def onKeepWithoutUploading(self, evt):
		self._finish(RESULT_KEEP_NO_UPLOAD)

	def _finish(self, resultCode):
		if self._recording:
			self._stopRecording()
		self._stopPreview()
		if not self._hasRecording:
			ui.message(_("Please record something first"))
			return
		try:
			self._flushVolumeIfDirty()
		except Exception as e:
			log.error("Cloud Uploader: could not apply volume before finishing: %s" % e)
		self.EndModal(resultCode)

	def onCancel(self, evt):
		if self._recording:
			self._timer.Stop()
			for recorder in (self._micRecorder, self._sysRecorder):
				if recorder is not None:
					try:
						recorder.abort()
					except Exception:
						pass
		self._stopPreview()
		self.EndModal(wx.ID_CANCEL)
		if hasattr(evt, "Veto"):
			evt.Veto()

	def GetOutputPaths(self):
		"""Returns the list of file(s) this recording should be uploaded
		or kept as: one combined file normally, or a microphone file plus
		a computer-audio file when both were recorded and separate
		tracks were requested."""
		if self._sourceMode == "both" and self.separateTracksCheckbox.GetValue():
			return [self._micOutputPath, self._sysOutputPath]
		return [self._outputPath]



def _addSectionHeader(helper, parent, text):
	header = wx.StaticText(parent, label=text)
	font = header.GetFont()
	font = font.Bold()
	header.SetFont(font)
	helper.addItem(header)


class CloudUploaderSettingsPanel(gui.settingsDialogs.SettingsPanel):
	title = _("Cloud Uploader")

	def makeSettings(self, settingsSizer):
		helper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer)

		# --- Uploading ---
		_addSectionHeader(helper, self, _("Uploading"))

		self._hostKeys = ["ask"] + [host.key for host in ALL_HOSTS]
		hostLabels = [_("Always ask")] + [host.label for host in ALL_HOSTS]
		self.hostChoice = helper.addLabeledControl(_("Default upload host:"), wx.Choice, choices=hostLabels)
		currentHostKey = config.conf["cloudUploader"]["defaultHost"]
		try:
			hostIndex = self._hostKeys.index(currentHostKey)
		except ValueError:
			hostIndex = 0
		self.hostChoice.SetSelection(hostIndex)

		self.autoCopyCheckbox = helper.addItem(
			wx.CheckBox(self, label=_("Automatically copy the link and skip the confirmation dialog"))
		)
		self.autoCopyCheckbox.SetValue(config.conf["cloudUploader"]["autoCopyOnComplete"])

		self.onlyWorkingCheckbox = helper.addItem(
			wx.CheckBox(self, label=_("After checking server status, show only the hosts that are working"))
		)
		self.onlyWorkingCheckbox.SetValue(config.conf["cloudUploader"]["showOnlyWorkingHosts"])

		self.maxHistoryCtrl = helper.addLabeledControl(
			_("Maximum number of links to keep in history:"),
			wx.SpinCtrl,
			min=1,
			max=200,
		)
		self.maxHistoryCtrl.SetValue(config.conf["cloudUploader"]["maxHistoryEntries"])

		# --- Voice recording ---
		_addSectionHeader(helper, self, _("Voice recording"))

		self._formatKeys = [key for key, ext, label in RECORDING_FORMATS]
		formatLabels = [label for key, ext, label in RECORDING_FORMATS]
		self.formatChoice = helper.addLabeledControl(_("Record in format:"), wx.Choice, choices=formatLabels)
		try:
			formatIndex = self._formatKeys.index(config.conf["cloudUploader"]["recordingFormat"])
		except ValueError:
			formatIndex = 0
		self.formatChoice.SetSelection(formatIndex)

		self._qualityKeys = [key for key, label, bitrate, rate, comp in AUDIO_QUALITY_LEVELS]
		qualityLabels = [label for key, label, bitrate, rate, comp in AUDIO_QUALITY_LEVELS]
		self.qualityChoice = helper.addLabeledControl(
			_("Audio quality (applies to MP3 and FLAC):"), wx.Choice, choices=qualityLabels
		)
		try:
			qualityIndex = self._qualityKeys.index(config.conf["cloudUploader"]["audioQuality"])
		except ValueError:
			qualityIndex = len(self._qualityKeys) - 1
		self.qualityChoice.SetSelection(qualityIndex)

		self._micDevices = _listInputDevices()
		micLabels = [name for _id, name in self._micDevices]
		self.micDeviceChoice = helper.addLabeledControl(_("Recording device:"), wx.Choice, choices=micLabels)
		configuredMicId = config.conf["cloudUploader"]["micDeviceId"]
		micIndex = 0
		for i, (devId, _name) in enumerate(self._micDevices):
			if devId == configuredMicId:
				micIndex = i
				break
		self.micDeviceChoice.SetSelection(micIndex)

		self._systemDevices = _listOutputDevices()
		systemLabels = [name for _id, name in self._systemDevices]
		self.systemDeviceChoice = helper.addLabeledControl(
			_("Computer audio comes from:"), wx.Choice, choices=systemLabels
		)
		configuredSystemId = config.conf["cloudUploader"]["systemDeviceId"] or None
		systemIndex = 0
		for i, (devId, _name) in enumerate(self._systemDevices):
			if devId == configuredSystemId:
				systemIndex = i
				break
		self.systemDeviceChoice.SetSelection(systemIndex)

		self._channelModeKeys = ["stereo", "mono"]
		channelModeLabels = [_("Stereo (if the device supports it)"), _("Mono")]
		self.channelModeChoice = helper.addLabeledControl(
			_("Recording channels:"), wx.Choice, choices=channelModeLabels
		)
		channelModeIndex = 1 if config.conf["cloudUploader"]["micPreferMono"] else 0
		self.channelModeChoice.SetSelection(channelModeIndex)

		self._sourceModeKeys = [key for key, label in RECORD_SOURCE_MODES]
		sourceModeLabels = [label for key, label in RECORD_SOURCE_MODES]
		self.sourceModeChoice = helper.addLabeledControl(
			_("Default what to record:"), wx.Choice, choices=sourceModeLabels
		)
		try:
			sourceModeIndex = self._sourceModeKeys.index(config.conf["cloudUploader"]["recordSourceMode"])
		except ValueError:
			sourceModeIndex = 0
		self.sourceModeChoice.SetSelection(sourceModeIndex)

		self.separateTracksCheckbox = helper.addItem(
			wx.CheckBox(
				self,
				label=_("When recording both microphone and computer audio, save them as separate tracks by default"),
			)
		)
		self.separateTracksCheckbox.SetValue(config.conf["cloudUploader"]["saveSeparateTracks"])

		try:
			defaultMicGain = int(round(float(config.conf["cloudUploader"]["micGainDb"])))
		except Exception:
			defaultMicGain = 0
		try:
			defaultSysGain = int(round(float(config.conf["cloudUploader"]["systemGainDb"])))
		except Exception:
			defaultSysGain = 0
		self.micGainSlider = helper.addLabeledControl(
			_("Default microphone volume when recording both (dB):"),
			wx.Slider,
			minValue=-20,
			maxValue=20,
			value=defaultMicGain,
			style=wx.SL_HORIZONTAL | wx.SL_AUTOTICKS,
		)
		self.systemGainSlider = helper.addLabeledControl(
			_("Default computer audio volume when recording both (dB):"),
			wx.Slider,
			minValue=-20,
			maxValue=20,
			value=defaultSysGain,
			style=wx.SL_HORIZONTAL | wx.SL_AUTOTICKS,
		)

		self.ffmpegPathCtrl = helper.addLabeledControl(
			_("Path to ffmpeg.exe, used to encode voice recordings to MP3/FLAC (leave blank to auto-detect from PATH):"),
			wx.TextCtrl,
		)
		self.ffmpegPathCtrl.SetValue(config.conf["cloudUploader"]["ffmpegPath"])

		self.autoStartRecordingCheckbox = helper.addItem(
			wx.CheckBox(
				self,
				label=_(
					"Start recording immediately when choosing \"Record a new voice clip\" "
					"(uses your default source, devices, and volumes; press Stop when finished)"
				),
			)
		)
		self.autoStartRecordingCheckbox.SetValue(config.conf["cloudUploader"]["autoStartRecording"])

		self.fileUploadOnlyCheckbox = helper.addItem(
			wx.CheckBox(
				self,
				label=_(
					"Hide voice recording; NVDA+alt+o always goes straight to choosing a file to upload"
				),
			)
		)
		self.fileUploadOnlyCheckbox.SetValue(config.conf["cloudUploader"]["fileUploadOnly"])

		# --- Recordings stored on this device ---
		_addSectionHeader(helper, self, _("Recordings stored on this device"))

		recordingsBtnSizer = wx.BoxSizer(wx.HORIZONTAL)
		openFolderBtn = wx.Button(self, label=_("&Open recordings folder"))
		openFolderBtn.Bind(wx.EVT_BUTTON, self.onOpenRecordingsFolder)
		recordingsBtnSizer.Add(openFolderBtn, flag=wx.RIGHT, border=10)
		clearRecordingsBtn = wx.Button(self, label=_("&Clear all recordings"))
		clearRecordingsBtn.Bind(wx.EVT_BUTTON, self.onClearRecordings)
		recordingsBtnSizer.Add(clearRecordingsBtn)
		helper.addItem(recordingsBtnSizer)

		# --- Other ---
		_addSectionHeader(helper, self, _("Other"))

		donateBtn = wx.Button(self, label=_("&Donate to support development"))
		donateBtn.Bind(wx.EVT_BUTTON, self.onDonate)
		helper.addItem(donateBtn)

	def onDonate(self, evt):
		webbrowser.open("https://ko-fi.com/naday")

	def onOpenRecordingsFolder(self, evt):
		try:
			_openRecordingsFolder()
		except Exception as e:
			log.error("Cloud Uploader: could not open recordings folder: %s" % e)
			gui.messageBox(
				_("Could not open the recordings folder: {error}").format(error=e),
				_("Error"),
				wx.OK | wx.ICON_ERROR,
				self,
			)

	def onClearRecordings(self, evt):
		folder = _getRecordingsFolder()
		if gui.messageBox(
			_("Remove all recordings stored on this device? This cannot be undone."),
			_("Clear all recordings"),
			wx.YES | wx.NO | wx.ICON_WARNING,
			self,
		) != wx.YES:
			return
		removed = 0
		try:
			names = os.listdir(folder)
		except Exception:
			names = []
		for name in names:
			path = os.path.join(folder, name)
			try:
				os.remove(path)
				removed += 1
			except Exception:
				pass
		gui.messageBox(
			_("Removed {n} recording(s).").format(n=removed),
			_("Clear all recordings"),
			wx.OK,
			self,
		)

	def onSave(self):
		config.conf["cloudUploader"]["defaultHost"] = self._hostKeys[self.hostChoice.GetSelection()]
		config.conf["cloudUploader"]["autoCopyOnComplete"] = self.autoCopyCheckbox.GetValue()
		config.conf["cloudUploader"]["maxHistoryEntries"] = self.maxHistoryCtrl.GetValue()
		config.conf["cloudUploader"]["showOnlyWorkingHosts"] = self.onlyWorkingCheckbox.GetValue()
		config.conf["cloudUploader"]["recordingFormat"] = self._formatKeys[self.formatChoice.GetSelection()]
		config.conf["cloudUploader"]["audioQuality"] = self._qualityKeys[self.qualityChoice.GetSelection()]
		config.conf["cloudUploader"]["ffmpegPath"] = self.ffmpegPathCtrl.GetValue()
		config.conf["cloudUploader"]["micDeviceId"] = self._micDevices[self.micDeviceChoice.GetSelection()][0]
		config.conf["cloudUploader"]["systemDeviceId"] = self._systemDevices[self.systemDeviceChoice.GetSelection()][0] or ""
		config.conf["cloudUploader"]["micPreferMono"] = self.channelModeChoice.GetSelection() == 1
		config.conf["cloudUploader"]["recordSourceMode"] = self._sourceModeKeys[self.sourceModeChoice.GetSelection()]
		config.conf["cloudUploader"]["saveSeparateTracks"] = self.separateTracksCheckbox.GetValue()
		config.conf["cloudUploader"]["micGainDb"] = float(self.micGainSlider.GetValue())
		config.conf["cloudUploader"]["systemGainDb"] = float(self.systemGainSlider.GetValue())
		config.conf["cloudUploader"]["autoStartRecording"] = self.autoStartRecordingCheckbox.GetValue()
		config.conf["cloudUploader"]["fileUploadOnly"] = self.fileUploadOnlyCheckbox.GetValue()


class TermsDialog(wx.Dialog):
	"""One-time (per terms version) notice about third-party upload hosts.

	Requires the person to check an "I understand" box before Agree becomes
	available. Disagreeing, or dismissing with Escape, closes the dialog
	without recording acceptance, so it is shown again on the next NVDA
	startup.
	"""

	def __init__(self, parent):
		super().__init__(
			parent,
			title=_("Cloud Uploader: upload hosts and terms of service"),
		)
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		helper = gui.guiHelper.BoxSizerHelper(self, sizer=mainSizer)

		text = helper.addItem(
			wx.TextCtrl(
				self,
				value=TERMS_TEXT,
				style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_BESTWRAP,
				size=(500, 250),
			)
		)
		text.SetFocus()

		self.understandCheckbox = helper.addItem(
			wx.CheckBox(
				self,
				label=_("I have read and understand the above"),
			)
		)
		self.understandCheckbox.Bind(wx.EVT_CHECKBOX, self.onCheckboxToggled)

		buttonHelper = gui.guiHelper.ButtonHelper(wx.HORIZONTAL)
		self.agreeButton = buttonHelper.addButton(self, label=_("&Agree"))
		self.agreeButton.Bind(wx.EVT_BUTTON, self.onAgree)
		self.agreeButton.Disable()
		self.disagreeButton = buttonHelper.addButton(self, label=_("&Disagree"))
		self.disagreeButton.Bind(wx.EVT_BUTTON, self.onDisagree)
		helper.addDialogDismissButtons(buttonHelper)

		self.Bind(wx.EVT_CLOSE, self.onClose)

		mainSizer.Fit(self)
		self.Sizer = mainSizer
		self.CentreOnScreen()

	def onCheckboxToggled(self, evt):
		self.agreeButton.Enable(self.understandCheckbox.GetValue())

	def onAgree(self, evt):
		if not self.understandCheckbox.GetValue():
			return
		config.conf["cloudUploader"]["termsAcceptedVersion"] = TERMS_VERSION
		self.EndModal(wx.ID_OK)

	def onDisagree(self, evt):
		# Do not record acceptance; the dialog will be shown again next startup.
		self.EndModal(wx.ID_CANCEL)

	def onClose(self, evt):
		# Escape / window close: same as Disagree, do not record acceptance.
		self.EndModal(wx.ID_CANCEL)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	scriptCategory = _("Cloud Uploader")

	def __init__(self):
		super().__init__()
		self._uploading = False
		self._progressDialog = None
		self._cancelEvent = None
		self._lastSpokenPercent = -10
		self._expiryLabel = None
		self._expirySeconds = None
		self._currentFileName = None
		self._currentHost = None
		self._currentPath = None
		self._currentExpiryCode = None
		self._history = _pruneExpired(_loadHistory())
		self._selectionDialog = None
		self._historyDialog = None
		self._pendingUploadPaths = []
		self._deleteAfterUploadFlag = False
		# Headless background recording (NVDA+shift+c) — independent of NVDA+alt+o.
		self._bgRecording = False
		self._bgProcessing = False
		self._bgMicRecorder = None
		self._bgSysRecorder = None
		self._bgSourceMode = "mic"
		self._bgSeparateTracks = False
		self._bgStartTime = None
		self._bgStartClockOffset = 0.0
		self._bgOutputPath = None
		self._bgMicOutputPath = None
		self._bgSysOutputPath = None
		self._bgPollScheduled = False
		_pruneOldRecordings()
		gui.NVDASettingsDialog.categoryClasses.append(CloudUploaderSettingsPanel)
		if config.conf["cloudUploader"]["termsAcceptedVersion"] != TERMS_VERSION:
			core.postNvdaStartup.register(self._showTermsIfNeeded)

	def _showTermsIfNeeded(self):
		core.postNvdaStartup.unregister(self._showTermsIfNeeded)
		if config.conf["cloudUploader"]["termsAcceptedVersion"] != TERMS_VERSION:
			wx.CallAfter(self._showTermsDialog)

	def _showTermsDialog(self):
		try:
			gui.mainFrame.prePopup()
			dlg = TermsDialog(gui.mainFrame)
			dlg.ShowModal()
			dlg.Destroy()
			gui.mainFrame.postPopup()
		except Exception:
			log.error("Cloud Uploader: could not show terms dialog", exc_info=True)

	def terminate(self):
		try:
			if self._bgRecording or self._bgMicRecorder is not None or self._bgSysRecorder is not None:
				self._abortBackgroundRecording()
		except Exception:
			pass
		try:
			gui.NVDASettingsDialog.categoryClasses.remove(CloudUploaderSettingsPanel)
		except Exception:
			pass
		super().terminate()

	def _focusExistingDialog(self, dlg):
		"""Best-effort attempt to bring dlg to the foreground. Native file
		dialogs don't reliably report IsShown()/support Raise() the way
		normal wx dialogs do, so this is not used to decide whether a
		dialog is open - only to try to help once we already know it is."""
		if dlg is None:
			return
		try:
			dlg.Raise()
			dlg.SetFocus()
		except Exception:
			pass

	def script_uploadFile(self, gesture):
		if self._uploading:
			ui.message(_("An upload is already in progress"))
			return
		if self._bgRecording or self._bgProcessing:
			ui.message(_("A background recording is in progress. Press NVDA+shift+c to stop it first."))
			return
		if self._selectionDialog is not None:
			self._focusExistingDialog(self._selectionDialog)
			ui.message(_("A file selection dialog is already open"))
			return
		if config.conf["cloudUploader"]["fileUploadOnly"]:
			wx.CallAfter(self._showFileDialog)
		else:
			wx.CallAfter(self._showSourceChoice)
	script_uploadFile.__doc__ = _(
		"Choose a file or record a voice clip, then upload it to the cloud and get a shareable direct download link"
	)

	def script_showLinkHistory(self, gesture):
		if self._historyDialog is not None:
			self._focusExistingDialog(self._historyDialog)
			ui.message(_("The upload history window is already open"))
			return
		wx.CallAfter(self._showLinkHistory)
	script_showLinkHistory.__doc__ = _("Show previously uploaded files so you can copy, open, or delete their links")

	def script_toggleBackgroundRecord(self, gesture):
		"""Start or stop a headless recording. First press starts capture with
		no window (using your default source and devices). Second press stops
		and opens the usual record dialog so you can preview, edit, and upload.
		This is independent of NVDA+alt+o."""
		try:
			if self._bgProcessing:
				ui.message(_("Still processing the previous recording, please wait"))
				return
			if self._bgRecording:
				self._stopBackgroundRecording()
				return
			if self._uploading:
				ui.message(_("An upload is already in progress"))
				return
			if self._selectionDialog is not None:
				self._focusExistingDialog(self._selectionDialog)
				ui.message(_("A Cloud Uploader dialog is already open"))
				return
			self._startBackgroundRecording()
		except Exception as e:
			log.error("Cloud Uploader: NVDA+shift+c failed: %s" % e, exc_info=True)
			ui.message(_("Cloud Uploader recording failed: {error}").format(error=e))
	script_toggleBackgroundRecord.__doc__ = _(
		"Start or stop background voice recording. First press starts recording with no window; "
		"second press stops and opens the record dialog"
	)

	def _scheduleBgPoll(self):
		"""Keeps feeding the waveIn/WASAPI buffers while a headless recording
		is active. Uses NVDA's core.callLater so it always runs on the main
		thread without depending on a wx.Timer owner."""
		if not self._bgRecording:
			self._bgPollScheduled = False
			return
		self._bgPollScheduled = True
		core.callLater(50, self._bgPollOnce)

	def _bgPollOnce(self):
		self._bgPollScheduled = False
		if not self._bgRecording:
			return
		if self._bgMicRecorder is not None:
			try:
				self._bgMicRecorder.poll()
			except Exception as e:
				log.error("Cloud Uploader: background mic poll failed: %s" % e)
		if self._bgSysRecorder is not None:
			try:
				self._bgSysRecorder.poll()
			except Exception as e:
				log.error("Cloud Uploader: background system poll failed: %s" % e)
		if self._bgRecording:
			self._scheduleBgPoll()

	def _abortBackgroundRecording(self):
		"""Stops devices and clears state without opening the record dialog."""
		self._bgRecording = False
		self._bgProcessing = False
		for rec in (self._bgMicRecorder, self._bgSysRecorder):
			if rec is not None:
				try:
					rec.abort()
				except Exception:
					pass
		self._bgMicRecorder = None
		self._bgSysRecorder = None

	def _startBackgroundRecording(self):
		sourceKey = config.conf["cloudUploader"]["recordSourceMode"] or "mic"
		if sourceKey not in ("mic", "computer", "both"):
			sourceKey = "mic"
		deviceId = config.conf["cloudUploader"]["micDeviceId"]
		systemDeviceId = config.conf["cloudUploader"]["systemDeviceId"] or None
		preferMono = config.conf["cloudUploader"]["micPreferMono"]
		separate = config.conf["cloudUploader"]["saveSeparateTracks"]

		micRecorder = None
		sysRecorder = None
		usedFallbackMicDevice = False
		usedFallbackSystemDevice = False
		try:
			if sourceKey in ("mic", "both"):
				micRecorder = _WaveInRecorder(deviceId=deviceId, preferMono=preferMono)
				micRecorder.open()
				usedFallbackMicDevice = micRecorder.usedFallbackDevice
			if sourceKey in ("computer", "both"):
				sysRecorder = _LoopbackRecorder(deviceId=systemDeviceId)
				sysRecorder.open()
				usedFallbackSystemDevice = sysRecorder.usedFallbackDevice
		except Exception as e:
			log.error("Cloud Uploader: could not open recording device for background capture: %s" % e)
			for rec in (micRecorder, sysRecorder):
				if rec is not None:
					try:
						rec.abort()
					except Exception:
						pass
			ui.message(_("Could not start recording: {error}").format(error=e))
			return

		sysStartClock = None
		micStartClock = None
		startErrors = []
		try:
			if sysRecorder is not None and micRecorder is not None:
				barrier = threading.Barrier(2)

				def _startSys():
					try:
						barrier.wait()
						sysRecorder.startCapture()
					except Exception as e:
						startErrors.append(e)
					finally:
						nonlocal sysStartClock
						sysStartClock = time.perf_counter()

				def _startMic():
					try:
						barrier.wait()
						micRecorder.startCapture()
					except Exception as e:
						startErrors.append(e)
					finally:
						nonlocal micStartClock
						micStartClock = time.perf_counter()

				sysThread = threading.Thread(target=_startSys)
				micThread = threading.Thread(target=_startMic)
				sysThread.start()
				micThread.start()
				sysThread.join()
				micThread.join()
				if startErrors:
					raise startErrors[0]
			elif sysRecorder is not None:
				sysRecorder.startCapture()
				sysStartClock = time.perf_counter()
			elif micRecorder is not None:
				micRecorder.startCapture()
				micStartClock = time.perf_counter()
		except Exception as e:
			log.error("Cloud Uploader: could not start background capture: %s" % e)
			for rec in (micRecorder, sysRecorder):
				if rec is not None:
					try:
						rec.abort()
					except Exception:
						pass
			ui.message(_("Could not start recording: {error}").format(error=e))
			return

		alias = "nvdaCloudUploaderRec%d" % int(time.time() * 1000)
		folder = _getRecordingsFolder()
		self._bgMicRecorder = micRecorder
		self._bgSysRecorder = sysRecorder
		self._bgSourceMode = sourceKey
		self._bgSeparateTracks = separate
		self._bgStartTime = time.time()
		if micStartClock is not None and sysStartClock is not None:
			self._bgStartClockOffset = micStartClock - sysStartClock
		else:
			self._bgStartClockOffset = 0.0
		self._bgOutputPath = os.path.join(folder, "%s.wav" % alias)
		self._bgMicOutputPath = os.path.join(folder, "%s_mic.wav" % alias)
		self._bgSysOutputPath = os.path.join(folder, "%s_system.wav" % alias)
		self._bgRecording = True
		self._scheduleBgPoll()

		if usedFallbackMicDevice and usedFallbackSystemDevice:
			ui.message(_("Background recording started using system default devices. Press NVDA+shift+c again to stop."))
		elif usedFallbackMicDevice:
			ui.message(_("Background recording started (default microphone). Press NVDA+shift+c again to stop."))
		elif usedFallbackSystemDevice:
			ui.message(_("Background recording started (default computer audio device). Press NVDA+shift+c again to stop."))
		else:
			ui.message(_("Background recording started. Press NVDA+shift+c again to stop."))

	def _stopBackgroundRecording(self):
		if not self._bgRecording:
			return
		self._bgRecording = False
		ui.message(_("Processing recording, please wait..."))
		self._bgProcessing = True
		try:
			micRaw = self._bgMicRecorder.stop() if self._bgMicRecorder else None
			sysRaw = self._bgSysRecorder.stop() if self._bgSysRecorder else None
			micChannels = self._bgMicRecorder.channels if self._bgMicRecorder else None
			micRate = self._bgMicRecorder.samplerate if self._bgMicRecorder else None
			micSampwidth = (self._bgMicRecorder.bitspersample // 8) if self._bgMicRecorder else 2
			sysRate = self._bgSysRecorder.samplerate if self._bgSysRecorder else None
		except Exception as e:
			log.error("Cloud Uploader: could not stop background recording: %s" % e)
			self._bgMicRecorder = None
			self._bgSysRecorder = None
			self._bgProcessing = False
			ui.message(_("Could not save the recording: {error}").format(error=e))
			return
		self._bgMicRecorder = None
		self._bgSysRecorder = None
		args = (
			micRaw, micChannels, micSampwidth, micRate,
			sysRaw, sysRate,
			self._bgSourceMode, self._bgSeparateTracks,
			self._bgOutputPath, self._bgMicOutputPath, self._bgSysOutputPath,
			self._bgStartClockOffset,
		)
		threading.Thread(target=self._bgProcessThread, args=args, daemon=True).start()

	def _bgProcessThread(
		self, micRaw, micChannels, micSampwidth, micRate,
		sysRaw, sysRate, sourceMode, separate, outPath, micPath, sysPath,
		startClockOffset,
	):
		try:
			result = _processCapturedAudio(
				micRaw, micChannels, micSampwidth, micRate,
				sysRaw, sysRate, sourceMode, separate,
				outPath, micPath, sysPath, startClockOffset,
			)
		except Exception as e:
			log.error("Cloud Uploader: could not process background recording: %s" % e)
			wx.CallAfter(self._bgProcessFailed, str(e))
			return
		wx.CallAfter(self._bgProcessDone, result)

	def _bgProcessFailed(self, errorDetail):
		self._bgProcessing = False
		ui.message(_("Could not save the recording: {error}").format(error=errorDetail))

	def _bgProcessDone(self, result):
		self._bgProcessing = False
		# Open the usual record dialog pre-loaded with this capture.
		wx.CallAfter(self._showRecordDialogWithPreload, result)

	def _showRecordDialogWithPreload(self, preload):
		gui.mainFrame.prePopup()
		try:
			recordDlg = RecordVoiceDialog(gui.mainFrame, autoStart=False, preload=preload)
			self._selectionDialog = recordDlg
			try:
				result = recordDlg.ShowModal()
				if result == wx.ID_CANCEL:
					return
				paths = recordDlg.GetOutputPaths()
			finally:
				recordDlg.Destroy()
				self._selectionDialog = None
		finally:
			gui.mainFrame.postPopup()
		paths = [p for p in paths if p and os.path.exists(p)]
		if not paths:
			return
		if result == RESULT_KEEP_NO_UPLOAD:
			ui.message(_("Recording kept without uploading"))
			return
		deleteAfterUpload = result == RESULT_UPLOAD_NO_SAVE
		self._startUploadQueue(paths, deleteAfterUpload)

	def _showSourceChoice(self):
		gui.mainFrame.prePopup()
		try:
			choiceDlg = wx.SingleChoiceDialog(
				gui.mainFrame,
				_("What would you like to upload?"),
				_("Cloud Uploader"),
				[_("Choose an existing file from disk"), _("Record a new voice clip")],
			)
			self._selectionDialog = choiceDlg
			try:
				if choiceDlg.ShowModal() != wx.ID_OK:
					return
				index = choiceDlg.GetSelection()
			finally:
				choiceDlg.Destroy()
				self._selectionDialog = None
		finally:
			gui.mainFrame.postPopup()
		if index == 0:
			wx.CallAfter(self._showFileDialog)
		elif index == 1:
			wx.CallAfter(self._showRecordDialog)

	def _showRecordDialog(self):
		gui.mainFrame.prePopup()
		try:
			try:
				autoStart = config.conf["cloudUploader"]["autoStartRecording"]
			except Exception:
				autoStart = False
			recordDlg = RecordVoiceDialog(gui.mainFrame, autoStart=autoStart)
			self._selectionDialog = recordDlg
			try:
				result = recordDlg.ShowModal()
				if result == wx.ID_CANCEL:
					return
				paths = recordDlg.GetOutputPaths()
			finally:
				recordDlg.Destroy()
				self._selectionDialog = None
		finally:
			gui.mainFrame.postPopup()
		paths = [p for p in paths if p and os.path.exists(p)]
		if not paths:
			return
		if result == RESULT_KEEP_NO_UPLOAD:
			ui.message(_("Recording kept without uploading"))
			return
		deleteAfterUpload = result == RESULT_UPLOAD_NO_SAVE
		self._startUploadQueue(paths, deleteAfterUpload)

	def _startUploadQueue(self, paths, deleteAfterUpload):
		paths = list(paths)
		if not paths:
			return
		self._pendingUploadPaths = paths[1:]
		self._deleteAfterUploadFlag = deleteAfterUpload
		self._encodeRecordingThenContinue(paths[0])

	def _encodeRecordingThenContinue(self, wavPath):
		formatKey = config.conf["cloudUploader"]["recordingFormat"]
		if formatKey == "wav":
			# Already a WAV file - nothing to encode, and no extra file with
			# a different extension gets left behind.
			self._startHostSelection(wavPath)
			return
		ffmpegPath = _findFfmpeg()
		if not ffmpegPath:
			wx.CallAfter(self._warnFfmpegNotFound, wavPath, formatKey)
			return
		ui.message(_("Encoding, please wait..."))
		thread = threading.Thread(target=self._encodeThread, args=(ffmpegPath, wavPath, formatKey), daemon=True)
		thread.start()

	def _warnFfmpegNotFound(self, wavPath, formatKey):
		formatLabel = dict((key, label) for key, ext, label in RECORDING_FORMATS).get(formatKey, formatKey)
		gui.messageBox(
			_(
				"Cloud Uploader could not find ffmpeg, so this recording will be uploaded as a WAV file "
				"instead of {format}. If ffmpeg is installed, open NVDA's settings, go to Cloud Uploader, and "
				"set the path to ffmpeg.exe there; leave it blank to have Cloud Uploader search your PATH "
				"and a few common install locations automatically."
			).format(format=formatLabel),
			_("ffmpeg not found"),
			wx.OK | wx.ICON_WARNING,
			gui.mainFrame,
		)
		self._startHostSelection(wavPath)

	def _encodeThread(self, ffmpegPath, wavPath, formatKey):
		ext = dict((key, ext) for key, ext, label in RECORDING_FORMATS).get(formatKey, ".mp3")
		outPath = os.path.splitext(wavPath)[0] + ext
		qualityKey = config.conf["cloudUploader"]["audioQuality"]
		preset = dict((key, (bitrate, rate, comp)) for key, label, bitrate, rate, comp in AUDIO_QUALITY_LEVELS).get(
			qualityKey, AUDIO_QUALITY_LEVELS[-1][2:]
		)
		bitrate, sampleRate, compressionLevel = preset
		try:
			if formatKey == "flac":
				_encodeToFlac(ffmpegPath, wavPath, outPath, compressionLevel, sampleRate)
			else:
				_encodeToMp3(ffmpegPath, wavPath, outPath, bitrate, sampleRate)
			# Keep only the file in the format the user chose - don't leave
			# the original WAV lying around alongside it.
			try:
				os.remove(wavPath)
			except Exception:
				pass
			wx.CallAfter(self._startHostSelection, outPath)
		except Exception as e:
			log.error("Cloud Uploader: %s encoding failed: %s" % (formatKey, e))
			wx.CallAfter(self._warnEncodingFailed, wavPath, str(e), formatKey)

	def _warnEncodingFailed(self, wavPath, errorDetail, formatKey):
		formatLabel = dict((key, label) for key, ext, label in RECORDING_FORMATS).get(formatKey, formatKey)
		gui.messageBox(
			_(
				"Encoding to {format} failed, so this recording will be uploaded as a WAV file instead. "
				"ffmpeg reported:\n\n{error}"
			).format(format=formatLabel, error=errorDetail or _("(no output)")),
			_("Encoding failed"),
			wx.OK | wx.ICON_WARNING,
			gui.mainFrame,
		)
		self._startHostSelection(wavPath)

	def _showFileDialog(self):
		gui.mainFrame.prePopup()
		try:
			fileDlg = wx.FileDialog(
				gui.mainFrame,
				message=_("Select a file to upload"),
				wildcard=_("All files") + " (*.*)|*.*",
				style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
			)
			self._selectionDialog = fileDlg
			try:
				if fileDlg.ShowModal() != wx.ID_OK:
					return
				path = fileDlg.GetPath()
			finally:
				fileDlg.Destroy()
				self._selectionDialog = None
		finally:
			gui.mainFrame.postPopup()
		if path:
			self._pendingUploadPaths = []
			self._deleteAfterUploadFlag = False
			self._startHostSelection(path)

	def _startHostSelection(self, path):
		hostKey = config.conf["cloudUploader"]["defaultHost"]
		host = HOSTS_BY_KEY.get(hostKey)
		if host is None:
			self._chooseHost(path)
		else:
			self._chooseExpiry(path, host)

	def _chooseHost(self, path):
		gui.mainFrame.prePopup()
		try:
			choiceDlg = HostChoiceDialog(gui.mainFrame, ALL_HOSTS)
			self._selectionDialog = choiceDlg
			try:
				if choiceDlg.ShowModal() != wx.ID_OK:
					return
				index = choiceDlg.GetSelection()
			finally:
				choiceDlg.Destroy()
				self._selectionDialog = None
		finally:
			gui.mainFrame.postPopup()
		if index < 0 or index >= len(ALL_HOSTS):
			return
		self._chooseExpiry(path, ALL_HOSTS[index])

	def _chooseExpiry(self, path, host):
		if len(host.expiryOptions) == 1:
			expiryLabel, expiryCode, seconds = host.expiryOptions[0]
			self._startUpload(path, host, expiryCode, expiryLabel, seconds)
			return
		gui.mainFrame.prePopup()
		try:
			choiceDlg = wx.SingleChoiceDialog(
				gui.mainFrame,
				_("How long should the download link stay active?"),
				_("Link expiry"),
				[label for label, code, seconds in host.expiryOptions],
			)
			self._selectionDialog = choiceDlg
			try:
				if choiceDlg.ShowModal() != wx.ID_OK:
					return
				index = choiceDlg.GetSelection()
			finally:
				choiceDlg.Destroy()
				self._selectionDialog = None
		finally:
			gui.mainFrame.postPopup()
		expiryLabel, expiryCode, seconds = host.expiryOptions[index]
		self._startUpload(path, host, expiryCode, expiryLabel, seconds)

	def _startUpload(self, path, host, expiryCode, expiryLabel, expirySeconds):
		self._uploading = True
		self._cancelEvent = _CancelToken()
		self._lastSpokenPercent = -10
		self._currentHost = host
		self._currentPath = path
		self._currentExpiryCode = expiryCode
		self._expiryLabel = expiryLabel
		self._expirySeconds = expirySeconds
		self._currentFileName = os.path.basename(path)
		gui.mainFrame.prePopup()
		self._progressDialog = UploadProgressDialog(
			gui.mainFrame,
			_("Uploading file"),
			_("Uploading {fileName}...").format(fileName=self._currentFileName),
		)
		self._progressDialog.setOnCancel(self._onProgressDialogCancelRequested)
		self._progressDialog.Show()
		self._progressDialog.SetFocus()
		thread = threading.Thread(target=self._uploadThread, args=(path, host, expiryCode), daemon=True)
		thread.start()

	def _onProgressDialogCancelRequested(self):
		if self._cancelEvent is not None:
			self._cancelEvent.set()

	def _uploadThread(self, path, host, expiryCode):
		try:
			link = host.upload(path, expiryCode, self._onProgress, self._cancelEvent)
		except UploadCancelledError:
			wx.CallAfter(self._onCancelled)
			return
		except Exception as e:
			log.error("Cloud Uploader: upload failed: %s" % e)
			wx.CallAfter(self._onError, str(e))
			return
		wx.CallAfter(self._onComplete, link)

	def _onProgress(self, percent):
		wx.CallAfter(self._updateProgress, percent)

	def _updateProgress(self, percent):
		if not self._progressDialog:
			return
		try:
			self._progressDialog.updateProgress(percent, _("Uploaded {percent}%").format(percent=percent))
		except RuntimeError:
			return
		if percent >= self._lastSpokenPercent + 10 or percent == 100:
			self._lastSpokenPercent = percent
			ui.message(_("Uploaded {percent} percent").format(percent=percent))

	def _closeProgressDialog(self):
		if self._progressDialog:
			dlg = self._progressDialog
			self._progressDialog = None
			try:
				dlg.stopTimer()
				dlg.Hide()
			except Exception:
				pass
			gui.mainFrame.postPopup()
			wx.CallAfter(dlg.Destroy)

	def _onComplete(self, link):
		self._closeProgressDialog()
		self._uploading = False
		now = datetime.datetime.now()
		expiresAt = now + datetime.timedelta(seconds=self._expirySeconds)
		entry = {
			"fileName": self._currentFileName or "",
			"link": link,
			"expiryLabel": self._expiryLabel or "",
			"uploadedAt": now.isoformat(),
			"expiresAt": expiresAt.isoformat(),
		}
		self._history.insert(0, entry)
		self._history = self._history[:_getMaxHistoryEntries()]
		_saveHistory(self._history)
		finishedPath = self._currentPath
		if config.conf["cloudUploader"]["autoCopyOnComplete"]:
			api.copyToClip(link, notify=False)
			ui.message(_("Upload complete, link copied to clipboard"))
			self._afterUploadCleanupAndContinue(finishedPath)
			return
		ui.message(_("Upload complete"))
		wx.CallAfter(self._showCompletionDialogThenContinue, link, entry["fileName"], finishedPath)

	def _showCompletionDialogThenContinue(self, link, fileName, finishedPath):
		self._showCompletionDialog(link, fileName)
		self._afterUploadCleanupAndContinue(finishedPath)

	def _afterUploadCleanupAndContinue(self, finishedPath):
		if self._deleteAfterUploadFlag and finishedPath:
			try:
				if os.path.exists(finishedPath):
					os.remove(finishedPath)
			except Exception as e:
				log.error("Cloud Uploader: could not remove recording after upload: %s" % e)
		if self._pendingUploadPaths:
			nextPath = self._pendingUploadPaths.pop(0)
			self._encodeRecordingThenContinue(nextPath)

	def _showCompletionDialog(self, link, fileName):
		gui.mainFrame.prePopup()
		try:
			dlg = LinkDialog(
				gui.mainFrame,
				_("File uploaded"),
				_("{fileName} uploaded - expires in {expiry}").format(fileName=fileName, expiry=self._expiryLabel),
				link,
			)
			dlg.ShowModal()
			wx.CallAfter(dlg.Destroy)
		finally:
			gui.mainFrame.postPopup()

	def _onError(self, message):
		self._closeProgressDialog()
		self._uploading = False
		gui.mainFrame.prePopup()
		try:
			dlg = UploadErrorDialog(gui.mainFrame, message)
			self._selectionDialog = dlg
			try:
				result = dlg.ShowModal()
			finally:
				dlg.Destroy()
				self._selectionDialog = None
		finally:
			gui.mainFrame.postPopup()
		if result == wx.ID_RETRY:
			self._startUpload(
				self._currentPath, self._currentHost, self._currentExpiryCode, self._expiryLabel, self._expirySeconds
			)
		elif result == _ID_CHOOSE_ANOTHER_HOST:
			self._chooseAnotherHost()
		else:
			# The user backed out of this upload rather than retrying it -
			# don't silently continue on to any other queued file.
			self._pendingUploadPaths = []
			self._deleteAfterUploadFlag = False

	def _chooseAnotherHost(self):
		path = self._currentPath
		if not path:
			return
		gui.mainFrame.prePopup()
		try:
			choiceDlg = HostChoiceDialog(gui.mainFrame, ALL_HOSTS, autoCheck=True)
			self._selectionDialog = choiceDlg
			try:
				if choiceDlg.ShowModal() != wx.ID_OK:
					return
				index = choiceDlg.GetSelection()
			finally:
				choiceDlg.Destroy()
				self._selectionDialog = None
		finally:
			gui.mainFrame.postPopup()
		if index < 0 or index >= len(ALL_HOSTS):
			return
		self._chooseExpiry(path, ALL_HOSTS[index])

	def _onCancelled(self):
		self._closeProgressDialog()
		self._uploading = False
		self._pendingUploadPaths = []
		self._deleteAfterUploadFlag = False
		ui.message(_("Upload cancelled"))

	def _showLinkHistory(self):
		self._history = _pruneExpired(self._history)
		gui.mainFrame.prePopup()
		try:
			dlg = LinkHistoryDialog(gui.mainFrame, self._history, self._onHistoryChanged)
			self._historyDialog = dlg
			dlg.ShowModal()
			self._historyDialog = None
			wx.CallAfter(dlg.Destroy)
		finally:
			gui.mainFrame.postPopup()

	def _onHistoryChanged(self, updatedHistory):
		self._history = updatedHistory
		_saveHistory(self._history)

	__gestures = {
		"kb:NVDA+alt+o": "uploadFile",
		"kb:NVDA+alt+l": "showLinkHistory",
		"kb:NVDA+shift+c": "toggleBackgroundRecord",
	}
