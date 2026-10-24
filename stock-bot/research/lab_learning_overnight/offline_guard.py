import sys,os
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def audit(event,args):
 if event in ('socket.connect','socket.getaddrinfo','subprocess.Popen','os.system'):raise PermissionError('offline research: network/process calls disabled')
 if event=='open':
  p,mode,flags=args
  writing=(isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
  if writing and isinstance(p,(str,bytes,os.PathLike)) and not Path(p).resolve().is_relative_to(ROOT):raise PermissionError('research write outside study denied')
sys.addaudithook(audit)
