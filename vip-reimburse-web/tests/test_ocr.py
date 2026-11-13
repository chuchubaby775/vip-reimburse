"""Fictitious invoice layouts; no real guest documents are used."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ocr import extract_fields,_result,_paddle_document

BUYER='91510100MA6ABC1234'
SELLER='91511000MA6XYZ5678'

class InvoiceExtractionTests(unittest.TestCase):
    def test_explicit_fields_and_gross_total(self):
        fields=extract_fields('发票号码：26112000000000000001\n开票日期：2026年11月13日\n购买方名称：示例会务科技有限公司\n购买方税号：'+BUYER+'\n销售方名称：示例交通服务有限公司\n销售方税号：'+SELLER+'\n不含税金额：100.00\n税额合计：6.00\n价税合计（大写）壹佰零陆元整 （小写）￥106.00')
        self.assertEqual(fields['invoiceNumber'],'26112000000000000001')
        self.assertEqual(fields['date'],'2026-11-13')
        self.assertEqual(fields['buyerName'],'示例会务科技有限公司')
        self.assertEqual(fields['sellerName'],'示例交通服务有限公司')
        self.assertEqual(fields['buyerTaxId'],BUYER)
        self.assertEqual(fields['sellerTaxId'],SELLER)
        self.assertEqual((fields['amount'],fields['netAmount'],fields['taxAmount']),(106,100,6))

    def test_side_by_side_text_preserves_parties(self):
        text='购买方信息                 销售方信息\n名称：示例会务科技有限公司      名称：示例交通服务有限公司\n统一社会信用代码/纳税人识别号：'+BUYER+'      统一社会信用代码/纳税人识别号：'+SELLER+'\n合计 ￥100.00   ￥6.00\n价税合计（大写）壹佰零陆元整 （小写）￥106.00'
        fields=extract_fields(text)
        self.assertEqual(fields['buyerName'],'示例会务科技有限公司')
        self.assertEqual(fields['sellerName'],'示例交通服务有限公司')
        self.assertEqual(fields['buyerTaxId'],BUYER)
        self.assertEqual(fields['sellerTaxId'],SELLER)
        self.assertEqual((fields['amount'],fields['netAmount'],fields['taxAmount']),(106,100,6))

    def test_geometry_handles_interleaved_reading_order(self):
        # OCR may return the two columns out of order. Explicit headings set ownership.
        blocks=[]
        for x,prefix,name,tax in [(.04,'购买方信息','示例会务科技有限公司',BUYER),(.55,'销售方信息','示例交通服务有限公司',SELLER)]:
            for y,text in [(.22,prefix),(.28,'名称：'+name),(.34,'纳税人识别号：'+tax)]:
                blocks.append({'text':text,'x':x,'y':y,'height':.025,'width':.4,'page':0,'confidence':.99})
        blocks.append({'text':'项目名称','x':.1,'y':.43,'height':.025,'page':0})
        text='\n'.join(b['text'] for b in reversed(blocks))
        fields=extract_fields(text,blocks)
        self.assertEqual(fields['buyerName'],'示例会务科技有限公司')
        self.assertEqual(fields['sellerName'],'示例交通服务有限公司')
        self.assertEqual(fields['buyerTaxId'],BUYER)
        self.assertEqual(fields['sellerTaxId'],SELLER)

    def test_old_vertical_layout(self):
        fields=extract_fields('购买方\n名 称：示例会务科技有限公司\n纳 税 人 识 别 号：'+BUYER+'\n项目名称\n会议服务\n价税合计（小写）：106.00\n销售方\n名 称：示例交通服务有限公司\n纳税人识别号：'+SELLER)
        self.assertEqual(fields['buyerName'],'示例会务科技有限公司')
        self.assertEqual(fields['sellerName'],'示例交通服务有限公司')
        self.assertEqual(fields['buyerTaxId'],BUYER)
        self.assertEqual(fields['sellerTaxId'],SELLER)

    def test_combined_heading_box_does_not_override_text_ownership(self):
        blocks=[{'text':'购买方信息             销售方信息','x':.05,'y':.2,'height':.03},
                {'text':'名称：示例会务科技有限公司 名称：示例交通服务有限公司','x':.05,'y':.25,'height':.03},
                {'text':'税号：'+BUYER+' 税号：'+SELLER,'x':.05,'y':.3,'height':.03}]
        fields=extract_fields('\n'.join(b['text'] for b in blocks),blocks)
        self.assertEqual(fields['buyerTaxId'],BUYER)
        self.assertEqual(fields['sellerTaxId'],SELLER)
        self.assertEqual(fields['sellerName'],'示例交通服务有限公司')

    def test_tax_id_internal_letters_are_preserved(self):
        tax='91510100TIN1234567A'
        result=_result('购买方\n名称：示例有限公司\n纳税人识别号：'+tax+'\n价税合计：106.00')
        self.assertEqual(result['fields']['buyerTaxId'],tax)
        self.assertFalse(any('尚无法明确' in item for item in result['warnings']))

    def test_spaced_labels_full_width_and_zero_tax(self):
        fields=extract_fields('发 票 号 码：１２３４５６７８\n开 票 日 期：２０２６ 年 １ 月 ３ 日\n购买方税号：９１５１０１００ＭＡ６ＡＢＣ１２３４\n价 税 合 计（小 写）：￥１，２３４．５０\n税 额：０．００')
        self.assertEqual(fields['invoiceNumber'],'12345678')
        self.assertEqual(fields['buyerTaxId'],BUYER)
        self.assertEqual(fields['date'],'2026-01-03')
        self.assertEqual(fields['amount'],1234.5)
        self.assertEqual(fields['taxAmount'],0)
        self.assertIn('taxAmount',_result('税额：0.00')['recognizedFields'])

    def test_unknown_and_invalid_date_are_not_guessed(self):
        fields=extract_fields('会议活动\n纳税人识别号：'+SELLER+'\n开票日期：2026-02-31\n不含税金额：100.00\n税额：免税')
        self.assertEqual(fields['buyerTaxId'],'')
        self.assertEqual(fields['sellerTaxId'],'')
        self.assertEqual(fields['date'],'')
        self.assertEqual(fields['amount'],'')
        self.assertEqual(fields['taxAmount'],'')
        self.assertEqual(fields['netAmount'],100)
        self.assertEqual(extract_fields('nothing legible')['amount'],'')
        self.assertTrue(any('尚无法明确' in item for item in _result('纳税人识别号：'+SELLER)['warnings']))

    def test_warning_does_not_autocorrect_tax_id(self):
        result=_result('购买方税号：91510100MA6ABC12O4\n不含税金额：100.00\n税额：6.00\n价税合计：120.00')
        self.assertEqual(result['fields']['buyerTaxId'],'91510100MA6ABC12O4')
        self.assertTrue(any('O/I' in item for item in result['warnings']))
        self.assertTrue(any('不等于' in item for item in result['warnings']))

    def test_table_tax_heading_does_not_capture_net_amount_on_next_line(self):
        text='金额\n税率\n税额\n100.00\n6%\n6.00\n税额合计：6.00\n价税合计：106.00'
        self.assertEqual(extract_fields(text)['taxAmount'],6)
        self.assertEqual(extract_fields('税额\n100.00')['taxAmount'],'')
        self.assertEqual(extract_fields('税额：3.00\n税额合计：6.00')['taxAmount'],6)

    def test_scanned_pdf_limits_pages_and_cleans_private_copy_on_error(self):
        from pypdf import PdfReader,PdfWriter
        class FailingEngine:
            def predict(self,input):
                self.source=Path(input)
                self.pages=len(PdfReader(input).pages)
                raise RuntimeError('synthetic inference failure')
        fake=FailingEngine()
        work=ROOT.parents[1]/'work';work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='ocr-tests-',dir=work) as tmp:
            source=Path(tmp)/'extensionless-private-file'
            writer=PdfWriter()
            for _ in range(6): writer.add_blank_page(width=200,height=200)
            with source.open('wb') as handle: writer.write(handle)
            with patch.dict(sys.modules,{'paddleocr':SimpleNamespace(PaddleOCR=lambda **kw:fake)}),patch('ocr._ENGINE',fake):
                with self.assertRaises(RuntimeError): _paddle_document(source,'application/pdf')
            self.assertEqual(fake.pages,5)
            self.assertFalse(fake.source.exists())
            self.assertFalse(fake.source.parent.exists())
            self.assertTrue(source.exists())

if __name__=='__main__': unittest.main()
