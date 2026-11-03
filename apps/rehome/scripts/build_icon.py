"""Generate the original ReHome icon using only the Python standard library."""
import struct
import zlib


def _inside(x,y,points):
    result=False;j=len(points)-1
    for i,(xi,yi) in enumerate(points):
        xj,yj=points[j]
        if (yi>y)!=(yj>y) and x<(xj-xi)*(y-yi)/(yj-yi)+xi:result=not result
        j=i
    return result


def png(size):
    raw=bytearray()
    house=[(42,119),(128,43),(214,119),(196,139),(186,130),(186,213),(70,213),(70,130),(60,139)]
    for row in range(size):
        raw.append(0)
        for col in range(size):
            samples=[]
            for dx,dy in ((.25,.25),(.75,.25),(.25,.75),(.75,.75)):
                x=(col+dx)*256/size;y=(row+dy)*256/size
                cx=min(max(x,56),200);cy=min(max(y,56),200)
                if (x-cx)**2+(y-cy)**2>56**2:samples.append((0,0,0,0));continue
                white=_inside(x,y,house) and not (112<=x<=144 and 157<=y<=213)
                samples.append((255,255,255,255) if white else (36,92,219,255))
            alpha=sum(c[3] for c in samples)//4
            if alpha:
                opaque=[c for c in samples if c[3]]
                raw.extend([sum(c[i] for c in opaque)//len(opaque) for i in range(3)]+[alpha])
            else:raw.extend((0,0,0,0))
    def chunk(tag,data):
        return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',size,size,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(bytes(raw),9))+chunk(b'IEND',b'')


def write_icon(destination):
    images=[(size,png(size)) for size in (16,24,32,48,64,128,256)]
    offset=6+16*len(images);directory=bytearray(struct.pack('<HHH',0,1,len(images)))
    for size,data in images:
        directory.extend(struct.pack('<BBBBHHII',size%256,size%256,0,0,1,32,len(data),offset));offset+=len(data)
    destination.write_bytes(bytes(directory)+b''.join(data for _,data in images))
