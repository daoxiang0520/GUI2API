"""Regenerate the tiny owned WebM fixture (optional developer OpenCV dependency)."""
from pathlib import Path
import cv2
import numpy as np

path=Path(__file__).with_name('resources.webm')
writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'VP80'),10,(64,48))
assert writer.isOpened(),'This developer OpenCV build needs a VP8 encoder'
try:
    for frame in range(10):
        pixels=np.full((48,64,3),(230,110,35),dtype=np.uint8)
        pixels[8:32,8:32]=(40,220,255)
        pixels[:,32:]=(60,150,30) if frame%2 else (60,40,200)
        writer.write(pixels)
finally:writer.release()
reader=cv2.VideoCapture(str(path));frames=0
try:
    while True:
        ok,frame=reader.read()
        if not ok:break
        assert frame.shape[:2]==(48,64);frames+=1
finally:reader.release()
assert frames==10,frames
print({'path':str(path),'bytes':path.stat().st_size,'decoded_frames':frames})
