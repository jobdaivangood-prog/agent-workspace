"""Small Unicode Markdown-to-RTF renderer for installation documents."""
import re

def escape(value):
    output = []
    for char in value:
        number = ord(char)
        if char in '\\{}':
            output.append('\\' + char)
        elif number < 128:
            output.append(char if number >= 32 or char == '\t' else ' ')
        else:
            units = char.encode('utf-16-le')
            for offset in range(0, len(units), 2):
                unit = int.from_bytes(units[offset:offset + 2], 'little')
                output.append('\\u' + str(unit if unit < 32768 else unit - 65536) + '?')
    return ''.join(output)

TOKEN = re.compile(r'\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)')

def inline(value):
    rich, plain, position = [], [], 0
    for match in TOKEN.finditer(value):
        rich.append(escape(value[position:match.start()]))
        plain.append(value[position:match.start()])
        if match.group(1) is not None:
            formatted, label = inline(match.group(1))
            rich.append('{\\b ' + formatted + '}')
        elif match.group(2) is not None:
            label = match.group(2)
            rich.append('{\\f1\\fs20 ' + escape(label) + '}')
        else:
            label, address = match.group(3), match.group(4).strip('<>')
            if any(char in address for char in '\r\n"'):
                raise ValueError('Invalid RTF hyperlink')
            rich.append('{\\field{\\*\\fldinst{HYPERLINK "' + escape(address) + '"}}{\\fldrslt{\\cf2\\ul ' + escape(label) + '}}}')
        plain.append(label)
        position = match.end()
    rich.append(escape(value[position:]))
    plain.append(value[position:])
    return ''.join(rich), ''.join(plain)

def render(markdown):
    body, expected, code = [], [], False
    for line in markdown.splitlines():
        if line.startswith('```'):
            code = not code
            continue
        if not line.strip():
            continue
        if code:
            body.append('\\pard\\cf0\\f1\\fs18\\li120\\sa0\\sl200\\slmult1 ' + escape(line) + '\\par\n')
            expected.append(line)
            continue
        heading = re.match(r'^(#{1,2})\s+(.*)$', line)
        if heading:
            rich, plain = inline(heading.group(2))
            size = 32 if len(heading.group(1)) == 1 else 26
            body.append('\\pard\\cf0\\f0\\fs' + str(size) + '\\b\\keepn\\sb200\\sa100 ' + rich + '\\b0\\par\n')
        else:
            text = line.strip()
            quote = text.startswith('> ')
            if quote:
                text = text[2:]
            rich, plain = inline(text)
            indent = '\\li360\\ri240\\cf1' if quote else '\\li360\\fi-360' if re.match(r'^\d+\. ', text) else '\\li0\\fi0'
            body.append('\\pard\\cf0\\f0\\fs22\\sa120\\sl264\\slmult1' + indent + ' ' + rich + '\\par\n')
        expected.append(plain)
    if code:
        raise ValueError('Unclosed code block')
    document = ('{\\rtf1\\ansi\\ansicpg1251\\deff0\\uc1\n'
                '{\\fonttbl{\\f0 Calibri;}{\\f1 Consolas;}}\n'
                '{\\colortbl;\\red65\\green65\\blue65;\\red0\\green80\\blue150;}\n'
                '\\paperw11906\\paperh16838\\margl1134\\margr1134\\margt1134\\margb1134\\widowctrl\\lang1049\n'
                + ''.join(body) + '}\n')
    return document.encode('ascii'), '\n'.join(expected) + '\n'
