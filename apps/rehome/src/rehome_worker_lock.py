"""Small OS-held UI worker lock, independent of historical trial tooling."""
import os
from pathlib import Path
import snapshot


class WorkerLock:
    def __init__(self,path):
        self.path,self.stream=Path(path),None

    def acquire(self):
        if os.name!='nt':raise ValueError('ReHome requires Windows')
        import msvcrt
        snapshot._safe_path(self.path,allow_missing=True)
        stream=self.path.open('a+b')
        try:
            stream.seek(0,os.SEEK_END)
            if stream.tell()==0:
                stream.write(b'0');stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            stream.close();raise
        self.stream=stream
        return self

    def release(self):
        if self.stream is not None:
            import msvcrt
            try:
                self.stream.seek(0);msvcrt.locking(self.stream.fileno(),msvcrt.LK_UNLCK,1)
            finally:
                self.stream.close();self.stream=None
