"""Pure visible-message projection; no files, network or native profile writes.

Known attachment blocks are omitted. Ambiguous text needs an exact, digest-bound
exclusion plan, never a heuristic rewrite. Credential detection is deliberately
limited: passing this filter is NOT approval to publish arbitrary real history.
"""
from __future__ import annotations
import hashlib
import json
import re


class HistoryReviewRequired(ValueError):
    pass


OMITTED = '[Source material omitted from transfer; request the original if needed.]'
ATTACHMENT_TYPES = frozenset(('image', 'localImage'))
_SENSITIVE = re.compile(
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{16,}|'
    r'(?i:authorization\s*[:=]\s*[\"\x27]?bearer\s+\S+)|'
    r'(?i:\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[\"\x27]?\S+)')
_EMBEDDED = re.compile(
    r'(?i:<\s*/?\s*(?:image|attached_file|file_attachment|document_content)\b|'
    r'data:(?:image|application|audio|video)/|# Files mentioned by the user:|Image attachment:|'
    r'<\s*(?:INSTRUCTIONS|environment_context|external_codex_apps_open_page)\b)')


def digest(item):
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HistoryReviewRequired('Duplicate JSON field in history')
        result[key] = value
    return result


def check_text(text):
    if not isinstance(text, str):
        raise HistoryReviewRequired('Invalid visible text')
    if _SENSITIVE.search(text):
        raise HistoryReviewRequired('Possible credential in visible text; explicit exclusion required')
    if _EMBEDDED.search(text):
        raise HistoryReviewRequired('Embedded material or supplied instructions require review')
    return text


def _literal_header_mention(text, match):
    """A precise prose code-literal, outside fenced or pending inline code."""
    if match.group(0) != '# Files mentioned by the user:': return False
    opening, closing = match.start()-1, match.end()
    if opening < 0 or closing >= len(text) or text[opening] != '`' or text[closing] != '`': return False
    if (opening > 0 and text[opening-1] in ('`','\\')) or (closing+1 < len(text) and text[closing+1]=='`'): return False
    line_start = text.rfind('\n',0,opening)+1
    line_end = text.find('\n',closing)
    if line_end < 0: line_end=len(text)
    line=text[line_start:line_end]
    if line.startswith('    ') or line.startswith('\t'): return False
    outside=text[line_start:opening]+text[closing+1:line_end]
    if not any(character.isalpha() for character in outside): return False
    fence, inline = None, None
    for line in text[:opening].splitlines():
        marker=re.match(r'^[ \t]*(`{3,}|~{3,})(.*)$',line)
        if fence:
            if marker and marker[1][0]==fence[0] and len(marker[1])>=fence[1] and not marker[2].strip(): fence=None
            continue
        # List/quote containers and unusual fence-like syntax are deliberately
        # not parsed as Markdown. Never grant an exemption across that ambiguity.
        if marker is None and re.search(r'`{3,}|~{3,}',line): return False
        if marker:
            if marker[1][0]=='`' and '`' in marker[2]: return False
            fence=(marker[1][0],len(marker[1]));inline=None;continue
        if not line.strip(): inline=None;continue
        for ticks in re.finditer(r'`+',line):
            before=line[:ticks.start()]
            slashes=len(before)-len(before.rstrip('\\'))
            if slashes%2: continue
            width=len(ticks[0])
            if inline is None:inline=width
            elif inline==width:inline=None
    return fence is None and inline is None


def visible_block_problems(text, *, role, block_index):
    if not isinstance(text,str) or role not in ('user','assistant') or type(block_index) is not int or block_index<0:
        raise HistoryReviewRequired('Invalid visible block context')
    problems=[]
    if _SENSITIVE.search(text): problems.append('possible_secret')
    for match in _EMBEDDED.finditer(text):
        if role=='assistant' and block_index==0 and _literal_header_mention(text,match): continue
        problems.append('embedded_source');break
    return problems


def check_visible_block(text, *, role, block_index):
    if visible_block_problems(text,role=role,block_index=block_index):
        raise HistoryReviewRequired('Visible block requires explicit review')
    return text


def check_projected_message(text, *, role, question_count=0):
    """Validate flattened cards conservatively; question portions remain strict."""
    if role=='assistant' and question_count:
        # The earliest renderer label cannot be later than the real card boundary.
        # Ambiguous labels in the main response therefore fail conservatively.
        boundary=text.find('\n\n[Вопрос 1]\n')
        if boundary<0: raise HistoryReviewRequired('Missing question projection boundary')
        check_visible_block(text[:boundary],role=role,block_index=0)
        check_text(text[boundary:])
        if _SENSITIVE.search(text):raise HistoryReviewRequired('Possible credential in projected text')
        return text
    return check_visible_block(text,role=role,block_index=0)


def agent_metadata(item):
    """Exact official async-question schema; missing/null options normalize alike."""
    if item.get('memoryCitation') is not None:
        raise HistoryReviewRequired('Memory citation requires a separate visible decoder')
    delivery=item.get('delivery')
    if delivery not in (None,'async'):
        raise HistoryReviewRequired('Unknown assistant delivery')
    questions=item.get('questions')
    if questions is not None:
        if not isinstance(questions,list):raise HistoryReviewRequired('Invalid assistant questions')
        normalized=[]
        for question in questions:
            if not isinstance(question,dict) or set(question)-{'title','options'} or not isinstance(question.get('title'),str):
                raise HistoryReviewRequired('Unknown question schema')
            options=question.get('options')
            if options is not None and (not isinstance(options,list) or any(not isinstance(option,str) for option in options)):
                raise HistoryReviewRequired('Unknown question option schema')
            normalized.append({'title':question['title'],'options':options})
        questions=normalized
    return {'delivery':delivery,'questions':questions,'memoryCitation':None}


def agent_blocks(item):
    metadata=agent_metadata(item)
    if not isinstance(item.get('text'),str):raise HistoryReviewRequired('Invalid assistant text')
    blocks=[item['text']]
    for question in metadata['questions'] or []:
        blocks.append(question['title']);blocks.extend(question['options'] or [])
    return blocks,metadata


def render_agent_blocks(blocks,metadata):
    # Labels are fixed renderer text; every source string was filtered before
    # insertion. This deliberately restores historical visible text, not UI state.
    output=blocks[0];cursor=1
    for number,question in enumerate(metadata['questions'] or [],1):
        output+='\n\n[Вопрос '+str(number)+']\n'+blocks[cursor];cursor+=1
        for option_number,_ in enumerate(question['options'] or [],1):
            output+='\n[Вариант '+str(option_number)+'] '+blocks[cursor];cursor+=1
    if cursor!=len(blocks):raise HistoryReviewRequired('Question block layout differs')
    return output


def _exclude(text, ranges):
    """Ranges address Unicode codepoints, not bytes; unknown/overlapping rejected."""
    if not isinstance(ranges, list):
        raise HistoryReviewRequired('Invalid exclusion ranges')
    cursor, parts = 0, []
    for pair in ranges:
        if (not isinstance(pair, list) or len(pair) != 2 or
                any(type(n) is not int for n in pair)):
            raise HistoryReviewRequired('Invalid exclusion range')
        start, end = pair
        if not cursor <= start < end <= len(text):
            raise HistoryReviewRequired('Overlapping or out-of-range exclusion')
        parts.extend((text[cursor:start], OMITTED))
        cursor = end
    parts.append(text[cursor:])
    return ''.join(parts)


RESTRICTED_TEXT = 'restricted_visible_text'
VERBATIM_TEXT = 'verbatim_visible_text'


def validate_text_policy(text_policy):
    if text_policy not in (RESTRICTED_TEXT, VERBATIM_TEXT):
        raise HistoryReviewRequired('Unknown visible text policy')
    return text_policy


def visible_message(item, exclusion=None, *, text_policy=RESTRICTED_TEXT):
    """Project typed visible fields; verbatim never enables opaque payloads."""
    validate_text_policy(text_policy)
    if text_policy == VERBATIM_TEXT and exclusion is not None:
        raise HistoryReviewRequired('Verbatim history cannot apply manual exclusions')
    if not isinstance(item, dict) or item.get('type') not in ('userMessage', 'agentMessage'):
        raise HistoryReviewRequired('Expected a visible message object')
    ident = item.get('id')
    if not isinstance(ident, str) or not ident:
        raise HistoryReviewRequired('Visible message lacks identity')
    excluded = {}
    if exclusion is not None:
        if (not isinstance(exclusion, dict) or set(exclusion) != {'message_sha256', 'text_ranges'} or
                exclusion['message_sha256'] != digest(item) or not isinstance(exclusion['text_ranges'], dict)):
            raise HistoryReviewRequired('Exclusion plan does not match the exact message')
        excluded = exclusion['text_ranges']
    applied, omitted, removed_ranges = set(), 0, 0

    def text(value, index):
        nonlocal removed_ranges
        if not isinstance(value, str):
            raise HistoryReviewRequired('Invalid visible text')
        key = str(index)
        if key in excluded:
            value = _exclude(value, excluded[key])
            removed_ranges += len(excluded[key])
            applied.add(key)
        if text_policy == VERBATIM_TEXT:
            return value
        return check_visible_block(value,role='assistant' if item['type']=='agentMessage' else 'user',block_index=index)

    if item['type'] == 'userMessage':
        content = item.get('content')
        if not isinstance(content, list):
            raise HistoryReviewRequired('User content must be a list')
        texts = []
        for index, part in enumerate(content):
            if not isinstance(part, dict):
                raise HistoryReviewRequired('Invalid user content block')
            if part.get('type') == 'text':
                if set(part) - {'type', 'text', 'text_elements'}:
                    raise HistoryReviewRequired('Unknown text metadata')
                if part.get('text_elements', []) != []:
                    raise HistoryReviewRequired('Annotated text requires a version-specific decoder')
                texts.append(text(part.get('text'), index))
            elif part.get('type') in ATTACHMENT_TYPES:
                omitted += 1
            else:
                raise HistoryReviewRequired('Unknown content block; no silent omission')
        result = {'id': ident, 'role': 'user', 'text': '\n'.join(texts), 'omitted_attachments': omitted}
    else:
        phase = item.get('phase')
        if phase not in (None, 'commentary', 'final_answer'):
            raise HistoryReviewRequired('Unknown assistant phase')
        blocks,metadata=agent_blocks(item)
        filtered=[text(value,index) for index,value in enumerate(blocks)]
        result = {'id': ident, 'role': 'assistant', 'text': render_agent_blocks(filtered,metadata), 'phase': phase}
    if set(excluded) != applied:
        raise HistoryReviewRequired('Exclusion plan contains an unused text block')
    detail={'message_id': ident, 'message_sha256': digest(item),
            'omitted_attachment_blocks': omitted, 'excluded_text_ranges': removed_ranges}
    if item['type']=='agentMessage' and (metadata['delivery'] is not None or metadata['questions'] is not None):
        detail['visible_card_projection']={'kind':'historical_question_card_as_text',
            'question_count':len(metadata['questions'] or []),
            'option_count':sum(len(question['options'] or []) for question in metadata['questions'] or []),
            'interactive_restoration':False}
    return result,detail
