"""Run inside the Steam container as default; creates then removes a test gamepad."""
import ctypes, ctypes.util, os, struct, fcntl, time
from pathlib import Path
# Create a temporary gamepad, just as Sunshine does, and verify hotplug through SDL.
os.environ['SDL_JOYSTICK_DISABLE_UDEV']='1'
sdl=ctypes.CDLL(ctypes.util.find_library('SDL2-2.0'))
sdl.SDL_JoystickOpen.restype=ctypes.c_void_p
sdl.SDL_JoystickGetButton.argtypes=[ctypes.c_void_p,ctypes.c_int]
sdl.SDL_JoystickClose.argtypes=[ctypes.c_void_p]
assert sdl.SDL_Init(0x200)==0
before=sdl.SDL_NumJoysticks()
fd=os.open('/dev/uinput',os.O_WRONLY|os.O_NONBLOCK)
try:
 for val in (1,3): fcntl.ioctl(fd,0x40045564,val)
 fcntl.ioctl(fd,0x40045565,0x130)
 for val in (0,1): fcntl.ioctl(fd,0x40045567,val)
 maxima=[32767]*2+[0]*62; minima=[-32768]*2+[0]*62
 os.write(fd,struct.pack('80sHHHHI',b'Steam containment test gamepad',3,0x45e,0x28e,1,0)+struct.pack('256i',*(maxima+minima+[0]*128)))
 fcntl.ioctl(fd,0x5501)
 for _ in range(40):
  sdl.SDL_PumpEvents(); sdl.SDL_JoystickUpdate()
  if sdl.SDL_NumJoysticks()>before:break
  time.sleep(.25)
 assert sdl.SDL_NumJoysticks()>before,'SDL did not detect hotplug'
 joystick=sdl.SDL_JoystickOpen(sdl.SDL_NumJoysticks()-1);assert joystick
 os.write(fd,struct.pack('llHHi',0,0,1,0x130,1)+struct.pack('llHHi',0,0,0,0,0))
 time.sleep(.2);sdl.SDL_JoystickUpdate()
 assert sdl.SDL_JoystickGetButton(joystick,0)==1,'Controller button event missing'
 sdl.SDL_JoystickClose(joystick)
 print('PASS: virtual gamepad hotplug and SDL button delivery')
finally:
 fcntl.ioctl(fd,0x5502);os.close(fd);sdl.SDL_Quit()
# Allow the sidecar to remove the destroyed test device.
time.sleep(0.75)
# Only virtual input nodes are published.
for p in Path('/dev/input').iterdir():
 assert '/devices/virtual/input/' in str((Path('/sys/class/input')/p.name).resolve()),p
print('PASS: physical host input devices absent')
