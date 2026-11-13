"""Offline invoice OCR and conservative field extraction; no document leaves this server."""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal, InvalidOperation
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unicodedata

FIELDS = ('invoiceNumber', 'date', 'buyerName', 'buyerTaxId', 'sellerName', 'sellerTaxId', 'netAmount', 'taxAmount', 'amount')
_LOCK = threading.Lock()
_ENGINE = None
_VISION = Path(__file__).resolve().parent / '.runtime' / 'vision-ocr'
_MONEY = r'([+-]?\d[\d,]*(?:\.\d{1,2})?)(?![\d.])'
_TAX_LABEL = r'(?:统一社会信用代码(?:/纳税人识别号)?|纳税人识别号|税号|(?<![A-Z0-9])(?:Tax\s*ID|TIN)(?![A-Z0-9]))'
_NAME_STOP = r'(?=(?:名称|纳税人识别号|统一社会信用代码|购买方|销售方|购方|销方|地址.?电话|开户行及账号|Name|Tax\s*ID)\s*:?|$)'


def capabilities():
    paddle = importlib.util.find_spec('paddleocr') is not None and importlib.util.find_spec('paddle') is not None
    pdf = importlib.util.find_spec('pypdf') is not None
    vision = _VISION.is_file()
    preferred = os.environ.get('OCR_ENGINE', 'auto').lower()
    engine = ('Apple Vision（本机离线）' if preferred == 'vision' and vision else 'PaddleOCR' if paddle else 'Apple Vision（本机离线）' if vision else '')
    return {'available': paddle or pdf or vision, 'engine': ' + '.join(filter(None, [engine, 'PDF文字提取' if pdf else ''])) or '未启用',
            'imageAvailable': paddle or vision, 'imageEngine': engine, 'fields': list(FIELDS),
            'message': '票据在本机识别，可提取双方税号、金额等字段，结果需要人工核对。' if paddle or vision else '电子 PDF 可提取文字；图片识别尚未启用，请手动填写并核对票面。'}


def _normalize(text):
    text = unicodedata.normalize('NFKC', str(text or '')).replace('\r', '\n')
    text = text.replace('∶', ':').replace('﹕', ':').replace('–', '-').replace('－', '-')
    return re.sub(r'(?<=[\u3400-\u9fff])[ \t]+(?=[\u3400-\u9fff])', '', text)


def _money(value):
    if value is None or value == '': return ''
    try:
        number = Decimal(str(value).replace(',', '').replace('，', '').replace(' ', ''))
        return float(number) if number.is_finite() and 0 <= number < 1_000_000 and number.as_tuple().exponent >= -2 else ''
    except (InvalidOperation, ValueError): return ''


def _tax_ids(text):
    found = []
    for match in re.finditer(_TAX_LABEL + r'\s*:?\s*', text, re.I):
        # Stop at labelled English fields, not letter sequences within a legal ID.
        tail = text[match.end():].split('\n', 1)[0]
        tail = re.split(r'(?:Name|Tax\s*ID|TIN)\s*:', tail, maxsplit=1, flags=re.I)[0]
        candidate = re.match(r'([A-Z0-9][A-Z0-9 \t-]{12,35})', tail, re.I)
        if not candidate: continue
        value = re.sub(r'[ \t]', '', candidate.group(1)).upper().rstrip('-')
        if 15 <= len(value) <= 24 and re.fullmatch(r'[A-Z0-9]+', value): found.append(value)
    return found


def _valid_name(value):
    value = value.strip(' :\t')
    # Redaction noise and another empty field's label are not organization names.
    if len(re.findall(r'[A-Za-z\u3400-\u9fff]', value)) < 2: return ''
    if re.match(_TAX_LABEL + r'|(?:购买方|销售方)?(?:名称|Name)\s*:?$', value, re.I): return ''
    return value


def _names(text):
    names = []
    for line in text.splitlines():
        for match in re.finditer(r'(?:名称|Name)\s*:?\s*(.{2,160}?)' + _NAME_STOP, line, re.I):
            value = match.group(1).strip(' :\t')
            value = _valid_name(value)
            if value: names.append(value)
    return names


def _party(region):
    names, taxes = _names(region), _tax_ids(region)
    return (names[0] if names else '', taxes[0] if taxes else '')


def _row_text(blocks):
    rows = []
    for box in sorted(blocks, key=lambda b: (b.get('page', 0), b['y'], b['x'])):
        row = next((r for r in rows if r[0].get('page', 0) == box.get('page', 0) and abs(r[0]['y'] - box['y']) < max(.009, min(r[0].get('height', .02), box.get('height', .02)) * .65)), None)
        if row is None: rows.append([box])
        else: row.append(box)
    return '\n'.join(' '.join(b['text'] for b in sorted(row, key=lambda b: b['x'])) for row in rows)


def _geometry_parties(blocks):
    result, clean = {}, []
    for b in blocks or []:
        if not isinstance(b, dict) or not isinstance(b.get('text'), str): continue
        try:
            box = {**b, 'text': _normalize(b['text']), 'x': float(b['x']), 'y': float(b['y']), 'height': float(b.get('height', .025))}
            if 0 <= box['x'] <= 1 and 0 <= box['y'] <= 1: clean.append(box)
        except (KeyError, TypeError, ValueError): continue
    for page in sorted({b.get('page', 0) for b in clean}):
        boxes = [b for b in clean if b.get('page', 0) == page]
        anchors = {}
        for key, pattern in [('buyer', r'购买方|购方'), ('seller', r'销售方|销方')]:
            candidates = [b for b in boxes if re.search(pattern, b['text'])]
            if not candidates:
                for b in boxes:
                    if b['text'] not in ('购', '销'): continue
                    stack = [c for c in boxes if abs(c['x']-b['x']) < .025 and b['y'] <= c['y'] <= b['y']+.25 and len(c['text']) <= 2]
                    if re.search(pattern, ''.join(c['text'] for c in sorted(stack, key=lambda c: c['y']))): candidates.append(b)
            if candidates: anchors[key] = min(candidates, key=lambda b: b['y'])
        if set(anchors) != {'buyer', 'seller'}: continue
        buyer, seller = anchors['buyer'], anchors['seller']
        if abs(buyer['x']-seller['x']) > .2 and abs(buyer['y']-seller['y']) < .16:
            split = (buyer['x']+seller['x'])/2
            top = min(buyer['y'], seller['y'])-.025
            table = [b['y'] for b in boxes if b['y'] > top+.07 and re.search(r'项目名称|货物或应税|服务名称|规格型号', b['text'])]
            bottom = min(table or [top+.32])
            for prefix, anchor in anchors.items():
                region = [b for b in boxes if top <= b['y'] < bottom and ((b['x'] < split) == (anchor['x'] < split))]
                name, tax = _party(_row_text(region))
                if name: result[prefix+'Name'] = name
                if tax: result[prefix+'TaxId'] = tax
        elif abs(buyer['y']-seller['y']) > max(.05, max(buyer['height'], seller['height'])*1.5):
            ordered = sorted(anchors.items(), key=lambda x: x[1]['y'])
            for index, (prefix, anchor) in enumerate(ordered):
                top = anchor['y']-.015
                limit = ordered[index+1][1]['y']-.02 if index+1 < len(ordered) else top+.26
                boundaries = [b['y'] for b in boxes if top+.04 < b['y'] < limit and re.search(r'项目名称|货物或应税|服务名称|规格型号', b['text'])]
                bottom = min(boundaries or [limit])
                name, tax = _party(_row_text([b for b in boxes if top <= b['y'] < bottom]))
                if name: result[prefix+'Name'] = name
                if tax: result[prefix+'TaxId'] = tax
    return result


def document_type(text):
    text = _normalize(text)
    if re.search(r'铁路电子客票', text): return 'rail_ticket'
    if '客票行程单' in text and '航' in text: return 'flight_itinerary'
    if all(label in text for label in ('航班号', '民航发展基金', '电子客票')): return 'flight_itinerary'
    return ''


def _ticket_column_value(blocks, label):
    """Associate a table value with its own heading; never use adjacent fare columns."""
    clean = []
    for block in blocks or []:
        try:
            b = {**block, 'text': _normalize(block['text']).strip(), **{k:float(block.get(k,0)) for k in ('x','y','width','height')}}
            if 0 <= b['x'] <= 1 and 0 <= b['y'] <= 1: clean.append(b)
        except (KeyError,TypeError,ValueError): continue
    headings = r'(?:票价|燃油附加费|增值税税率|增值税税额|税额|民航发展基金|其他税费|合计)'
    found = []
    for anchor in clean:
        if not re.fullmatch(label+r'\s*[:：]?', anchor['text']): continue
        page = anchor.get('page',0)
        peers = [b for b in clean if b.get('page',0)==page and abs(b['y']-anchor['y']) < max(.018,anchor['height']*.65) and re.fullmatch(headings,b['text'])]
        right = min([b['x'] for b in peers if b['x'] > anchor['x']+.015] or [1.01])
        candidates = []
        for b in clean:
            if b is anchor or b.get('page',0)!=page: continue
            # A decimal price or explicit currency is required; bare ticket IDs do not qualify.
            m = re.fullmatch(r'(?:CNY|RMB|[¥￥$])\s*'+_MONEY, b['text'], re.I) or re.fullmatch(r'(\d[\d,]*\.\d{2})', b['text'])
            if not m: continue
            center = b['x']+b['width']/2
            same_row = abs(b['y']-anchor['y']) < max(.012,min(b['height'],anchor['height'])*.6) and b['x'] >= anchor['x']+anchor['width']-.015
            below = .008 <= b['y']-anchor['y'] <= .12 and center >= anchor['x']-.01
            if center < right and (same_row or below): candidates.append(_money(m.group(1)))
        if len(candidates)==1 and candidates[0]!='': found.append(candidates[0])
    return found[0] if len(found)==1 else ''


def _ticket_fields(text, blocks, kind):
    if not kind: return {}
    result = {}
    label = r'票价' if kind=='rail_ticket' else r'(?<![税额金额])合计'
    currency = r'[ \t]*:?[ \t]*(?:CNY|RMB|[¥￥$])?[ \t]*'
    matches = list(re.finditer(label+currency+_MONEY, text, re.I))
    # Airline headers may share a row with other amounts. Require an explicit pair.
    if len(matches)==1: result['amount'] = _money(matches[0].group(1))
    if result.get('amount','')=='': result['amount'] = _ticket_column_value(blocks, r'票价' if kind=='rail_ticket' else r'合计')
    if kind=='flight_itinerary':
        tax = _ticket_column_value(blocks,r'(?:增值税)?税额')
        if tax!='': result['taxAmount'] = tax
    return {key:value for key,value in result.items() if value!=''}


def extract_fields(text, blocks=None):
    """Use explicit labels plus layout; never infer missing monetary values."""
    text = _normalize(text)
    kind = document_type(text)
    fields = {x: '' for x in FIELDS}
    invoice = re.search(r'(?:发票号码|发票号|Invoice\s*(?:No\.?|Number))\s*[:#]?\s*([A-Z0-9][A-Z0-9 \t]{7,34})(?![A-Z0-9])', text, re.I)
    if invoice:
        value = re.sub(r'[ \t]', '', invoice.group(1)).strip()
        if 8 <= len(value) <= 30: fields['invoiceNumber'] = value
    date_labels = r'开票日期|开票时间|Invoice\s*Date' + (r'|填开日期' if kind=='flight_itinerary' else '' if kind else r'|Date')
    date = re.search(r'(?:'+date_labels+r')\s*:?\s*(\d{4})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*日?', text, re.I)
    if date:
        try: fields['date'] = datetime(*map(int, date.groups())).date().isoformat()
        except ValueError: pass
    for prefix, label in [('buyer', r'(?:购买方|购方|Buyer)'), ('seller', r'(?:销售方|销方|Seller)')]:
        name = re.search(label + r'\s*(?:名称|Name)\s*:?\s*(.{2,160}?)' + _NAME_STOP, text, re.I)
        if name: fields[prefix+'Name'] = _valid_name(name.group(1))
        tax = re.search(label+r'\s*'+_TAX_LABEL+r'\s*:?\s*([A-Z0-9][A-Z0-9 \t]{13,27})', text, re.I)
        if tax:
            value = re.sub(r'[ \t]', '', tax.group(1)).upper()
            if 15 <= len(value) <= 24: fields[prefix+'TaxId'] = value
    side = next((line for line in text.splitlines() if re.search(r'购买方|购方', line) and re.search(r'销售方|销方', line)), '')
    if side:
        order = ['buyer', 'seller'] if re.search(r'购买方|购方', side).start() < re.search(r'销售方|销方', side).start() else ['seller', 'buyer']
        for line in text.splitlines():
            names, taxes = _names(line), _tax_ids(line)
            for values, suffix in [(names, 'Name'), (taxes, 'TaxId')]:
                if len(values) == 2:
                    for prefix, value in zip(order, values):
                        if not fields[prefix+suffix]: fields[prefix+suffix] = value
    else:
        headers = list(re.finditer(r'购买方(?:信息)?|购方(?:信息)?|销售方(?:信息)?|销方(?:信息)?', text))
        for i, header in enumerate(headers):
            prefix = 'buyer' if header.group(0).startswith('购') else 'seller'
            region = text[header.end():headers[i+1].start() if i+1<len(headers) else header.end()+700]
            name, tax = _party(region)
            if name and not fields[prefix+'Name']: fields[prefix+'Name'] = name
            if tax and not fields[prefix+'TaxId']: fields[prefix+'TaxId'] = tax
    fields.update(_geometry_parties(blocks))
    # A table heading must not consume a different column's amount on the next
    # OCR line. Prefer explicit totals and accept label/value pairs on one row.
    currency = r'[ \t]*:?[ \t]*(?:CNY|RMB|[¥￥$])?[ \t]*'
    patterns = {
        'amount': [r'(?:\(?小写\)?|Total\s*(?:Amount|Due)|Grand\s*Total)'+currency+_MONEY, r'价税合计(?!\s*\(?大写)'+currency+_MONEY],
        'netAmount': [r'(?:不含税金额|金额合计|合计金额|Net\s*Amount|Subtotal)'+currency+_MONEY],
        'taxAmount': [r'(?:税额合计|合计税额|Tax\s*Amount)'+currency+_MONEY, r'税额'+currency+_MONEY],
    }
    for field, regexes in patterns.items():
        for pattern in regexes:
            match = re.search(pattern, text, re.I)
            if match:
                fields[field] = _money(match.group(1))
                if fields[field] != '': break
    for line in text.splitlines():
        match = re.match(r'^\s*合计\s*[¥￥$]?\s*'+_MONEY+r'\s+[¥￥$]?\s*'+_MONEY+r'\s*$', line)
        if match:
            if fields['netAmount'] == '': fields['netAmount'] = _money(match.group(1))
            if fields['taxAmount'] == '': fields['taxAmount'] = _money(match.group(2))
    for key,value in _ticket_fields(text,blocks,kind).items():
        if fields[key]=='': fields[key]=value
    return fields


def _warnings(fields, blocks=None, text=''):
    hints = []
    for prefix, label in [('buyer', '购买方'), ('seller', '销售方')]:
        value = fields[prefix+'TaxId']
        if value and len(value) not in (15,18,20): hints.append(f'{label}税号长度与常见格式不一致，请逐位核对。')
        if value and re.search(r'[OI]', value): hints.append(f'{label}税号含易混淆的字母 O/I，请对照原票核对。')
    if fields['amount'] == '': hints.append('未提取到票面总金额，请手动核对报销金额。')
    known_ids = {fields['buyerTaxId'], fields['sellerTaxId']} - {''}
    if set(_tax_ids(_normalize(text))) - known_ids:
        hints.append('票面有税号尚无法明确对应购买方或销售方，请对照原票填写。')
    if all(fields[x] != '' for x in ('amount','netAmount','taxAmount')):
        if abs(Decimal(str(fields['amount']))-Decimal(str(fields['netAmount']))-Decimal(str(fields['taxAmount']))) > Decimal('.02'):
            hints.append('不含税金额与税额之和不等于价税合计，请核对识别结果。')
    if blocks and any(float(b.get('confidence',1)) < .65 for b in blocks if isinstance(b,dict)):
        hints.append('部分文字清晰度较低，请重点核对税号、发票号码和金额。')
    return hints


def _result(text='', engine='', blocks=None, status='done', message='', warnings=None):
    fields = extract_fields(text,blocks) if text else {key:'' for key in FIELDS}
    recognized = [key for key,value in fields.items() if value != '']
    hints = list(warnings or [])+(_warnings(fields,blocks,text) if status=='done' else [])
    if status=='done' and not recognized: message='已读取文字，但未提取到明确的发票字段，请手动填写或上传更清晰的票据。'
    elif not message: message=f'已提取 {len(recognized)} 项票据信息，请对照原票核对；空白字段需手动填写。'
    kind = document_type(text)
    ticket = {'rail_ticket':('rail','电子发票（铁路电子客票）'),'flight_itinerary':('flight','电子发票（航空运输电子客票行程单）')}.get(kind,('',''))
    return {'documentType':kind,'suggestedCategory':ticket[0],'documentLabel':ticket[1],'status':status,'engine':engine,'fields':fields,'recognizedFields':recognized,'warnings':list(dict.fromkeys(hints)),'message':message}


def _paddle_document(path,mime):
    global _ENGINE
    cache=Path(__file__).resolve().parent/'.runtime'/'paddle'
    for key,value in {
        'PADDLE_PDX_CACHE_HOME':str(cache/'paddlex'), 'PADDLE_HOME':str(cache/'paddle'),
        'XDG_CACHE_HOME':str(cache/'cache'), 'HF_HOME':str(cache/'huggingface'),
        'MODELSCOPE_CACHE':str(cache/'modelscope'), 'PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK':'True',
        'PADDLE_PDX_MODEL_SOURCE':'BOS', 'HF_HUB_DISABLE_TELEMETRY':'1',
    }.items(): os.environ.setdefault(key,value)
    from paddleocr import PaddleOCR
    with _LOCK:
        if _ENGINE is None:
            _ENGINE=PaddleOCR(text_detection_model_name='PP-OCRv5_mobile_det',text_recognition_model_name='PP-OCRv5_mobile_rec',
                              use_doc_orientation_classify=False,use_doc_unwarping=False,use_textline_orientation=False,
                              device='cpu',cpu_threads=4,enable_mkldnn=False)
        if hasattr(_ENGINE,'predict'):
            lines,blocks=[],[]
            temp=None
            try:
                if mime.startswith('image/'):
                    from PIL import Image
                    import numpy as np
                    with Image.open(path) as image: source=np.asarray(image.convert('RGB'))[:,:,::-1].copy()
                else:
                    # Storage IDs have no extension; Paddle's PDF loader needs one.
                    temp=tempfile.TemporaryDirectory(prefix='ocr-',dir=Path(path).parent)
                    source=str(Path(temp.name)/'invoice.pdf')
                    from pypdf import PdfReader, PdfWriter
                    writer=PdfWriter()
                    for page in PdfReader(str(path)).pages[:5]: writer.add_page(page)
                    with open(source,'wb') as handle: writer.write(handle)
                    os.chmod(source,0o600)
                output=list(_ENGINE.predict(input=source))
            finally:
                if temp is not None: temp.cleanup()
            for page_index,result in enumerate(output):
                data=result.json if hasattr(result,'json') else result
                if callable(data): data=data()
                if isinstance(data,str): data=json.loads(data)
                if not isinstance(data,dict): continue
                data=data.get('res',data)
                texts=data.get('rec_texts',[]);scores=data.get('rec_scores',[]);polys=data.get('rec_polys',data.get('dt_polys',[]))
                lines.extend(texts)
                points=[point for poly in polys for point in poly]
                width=max((float(p[0]) for p in points),default=1)+10; height=max((float(p[1]) for p in points),default=1)+10
                for index,(text,poly) in enumerate(zip(texts,polys)):
                    xs,ys=[float(p[0]) for p in poly],[float(p[1]) for p in poly]
                    blocks.append({'text':text,'confidence':float(scores[index]) if index<len(scores) else 1,'x':min(xs)/width,'y':min(ys)/height,'width':(max(xs)-min(xs))/width,'height':(max(ys)-min(ys))/height,'page':page_index})
            return '\n'.join(lines),blocks
        output=_ENGINE.ocr(str(path),cls=True)
        return '\n'.join(item[1][0] for page in (output or []) for item in (page or []) if len(item)>1),[]


def _vision_document(path,mime):
    process=subprocess.run([str(_VISION),str(path),mime],capture_output=True,text=True,timeout=90,check=True)
    data=json.loads(process.stdout)
    return '\n'.join(line['text'] for line in data.get('lines',[])),data.get('blocks',[])


def _pdf_document(reader):
    texts,blocks=[],[]
    for index,page in enumerate(reader.pages):
        width,height=float(page.mediabox.width),float(page.mediabox.height)
        def collect(text,cm,tm,font,size):
            if not text.strip(): return
            x,y=tm[4]*cm[0]+tm[5]*cm[2]+cm[4],tm[4]*cm[1]+tm[5]*cm[3]+cm[5]
            for offset,line in enumerate(text.splitlines()):
                if line.strip(): blocks.append({'text':line.strip(),'x':max(0,x/width),'y':max(0,1-(y-size*offset)/height),'width':min(1,len(line)*size*.6/width),'height':max(.008,size/height),'confidence':1,'page':index})
        native=page.extract_text(visitor_text=collect) or ''
        try: layout=page.extract_text(extraction_mode='layout') or native
        except Exception: layout=native
        texts.append(layout)
    return '\n'.join(texts),blocks


def recognize(path,mime):
    info=capabilities()
    if mime=='application/pdf':
        try:
            from pypdf import PdfReader
            reader=PdfReader(str(path))
            if reader.is_encrypted: return _result(engine='PDF文字提取',status='failed',message='PDF 已加密，请上传未加密版本或手动填写。')
            if len(reader.pages)>30: return _result(engine='PDF文字提取',status='failed',message='PDF 超过 30 页，请按票据拆分上传。')
            text,boxes=_pdf_document(reader)
            if len(text.strip())>=12:
                native=_result(text,'PDF文字提取',boxes)
                if native['recognizedFields'] or not info['imageAvailable']: return native
        except ImportError: pass
        except Exception: return _result(engine='PDF文字提取',status='failed',message='无法读取这份 PDF，请手动填写或上传清晰图片。')
    if not info['imageAvailable']: return _result(engine=info['engine'],status='unavailable',message='图片或扫描件识别尚未启用，请对照原票手动填写。')
    engine=info['imageEngine'];warnings=[]
    try:
        if engine.startswith('Apple Vision'): text,boxes=_vision_document(path,mime)
        else:
            try: text,boxes=_paddle_document(path,mime)
            except Exception:
                if not _VISION.is_file(): raise
                text,boxes=_vision_document(path,mime);engine='Apple Vision（本机离线）';warnings.append('PaddleOCR 本次未完成，已使用本机离线识别。')
        if not text.strip(): return _result(engine=engine,status='failed',message='没有识别到清晰文字，请手动填写。')
        if mime=='application/pdf': warnings.append('扫描 PDF 最多识别前 5 页，建议每张票据单独上传。')
        return _result(text,engine,boxes,warnings=warnings)
    except Exception: return _result(engine=engine,status='failed',message='本机识别未完成，请手动填写；原文件已保留。')
