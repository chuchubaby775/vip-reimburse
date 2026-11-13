"""Synthetic transport invoice layouts; user samples are never included in source."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ocr import _result,extract_fields


def aviation_blocks(total='CNY670.00'):
    labels=[('票价',.25),('燃油附加费',.36),('增值税税率',.46),('增值税税额',.54),('民航发展基金',.65),('其他税费',.75),('合计',.86)]
    values=[('CNY550.46',.28),('CNY18.35',.40),('9%',.52),('CNY51.19',.58),('CNY50.00',.68),('CNY0.00',.79),(total,.92)]
    return [dict(text=t,x=x,y=.70,width=.03,height=.035,page=0) for t,x in labels]+[dict(text=t,x=x,y=.74,width=.07,height=.03,page=0) for t,x in values]


class TicketExtractionTests(unittest.TestCase):
    def test_rail_fare_and_invoice_date_are_separate_from_travel_date(self):
        r=_result('电子发票（铁路电子客票）\n2026年11月17日 12:02开\n票价：￥493.00\n开票日期：2026年12月02日\n发票号码：26123456789012345678\n购买方名称：虚构会务有限公司\n统一社会信用代码：91510100MA6ABC1234')
        self.assertEqual((r['documentType'],r['suggestedCategory']),('rail_ticket','rail'))
        self.assertEqual(r['fields']['amount'],493)
        self.assertEqual(r['fields']['date'],'2026-12-02')
        self.assertEqual(r['fields']['buyerTaxId'],'91510100MA6ABC1234')
        self.assertEqual(r['fields']['buyerName'],'虚构会务有限公司')
        self.assertEqual(r['fields']['invoiceNumber'],'26123456789012345678')

    def test_aviation_table_reads_total_not_fare_and_uses_issued_date(self):
        blocks=aviation_blocks()
        # Deliberately disordered OCR output, as cells need not be read row by row.
        text='电子发票（航空运输电子客票行程单）\n2026年11月16日\n2027年11月13日\n填开日期：2026年12月01日\n'+'\n'.join(b['text'] for b in reversed(blocks))
        r=_result(text,blocks=blocks)
        self.assertEqual((r['documentType'],r['suggestedCategory']),('flight_itinerary','flight'))
        self.assertEqual((r['fields']['amount'],r['fields']['taxAmount']),(670,51.19))
        self.assertEqual(r['fields']['date'],'2026-12-01')
        self.assertEqual(r['fields']['netAmount'],'')
        self.assertEqual(r['fields']['sellerName'],'')

    def test_aviation_explicit_cny_and_tax_pairs(self):
        f=extract_fields('电子发票（航空运输电子客票行程单）\n票价 CNY550.46\n燃油附加费 CNY18.35\n增值税税额 CNY51.19\n民航发展基金 CNY50.00\n合计 CNY670.00')
        self.assertEqual((f['amount'],f['taxAmount']),(670,51.19))
        self.assertEqual(f['netAmount'],'')

    def test_unreadable_total_never_falls_back_to_fare_or_other_taxes(self):
        blocks=aviation_blocks('CNY ***')
        f=extract_fields('电子发票（航空运输电子客票行程单）\n票价 CNY550.46\n其他税费 CNY0.00',blocks)
        self.assertEqual(f['amount'],'')
        self.assertEqual(f['taxAmount'],51.19)

    def test_unrelated_lower_row_amount_is_not_a_table_total(self):
        blocks=aviation_blocks('')
        blocks.append(dict(text='CNY800.00',x=.92,y=.94,width=.07,height=.03,page=0))
        self.assertEqual(extract_fields('电子发票（航空运输电子客票行程单）',blocks)['amount'],'')

    def test_ambiguous_or_cross_page_table_values_are_not_guessed(self):
        blocks=aviation_blocks()
        blocks[-1]['page']=1
        self.assertEqual(extract_fields('电子发票（航空运输电子客票行程单）',blocks)['amount'],'')
        blocks=aviation_blocks()+[dict(text='CNY800.00',x=.92,y=.78,width=.07,height=.03,page=0)]
        self.assertEqual(extract_fields('电子发票（航空运输电子客票行程单）',blocks)['amount'],'')

    def test_redacted_names_and_empty_tax_labels_are_not_organizations(self):
        for value in ['/.','***','统一社会信用代码/纳税人识别号：','统一社会信用代码：']:
            f=extract_fields('电子发票（航空运输电子客票行程单）\n购买方名称：'+value)
            self.assertEqual(f['buyerName'],'',value)
            self.assertEqual(f['buyerTaxId'],'',value)

    def test_ticket_numbers_and_issuer_are_not_guessed_as_invoice_or_seller(self):
        f=extract_fields('电子发票（航空运输电子客票行程单）\n电子客票号码：9991234567890\n填开单位：虚构代理服务有限公司\n购买方名称：\n统一社会信用代码/纳税人识别号：')
        self.assertEqual(f['invoiceNumber'],'')
        self.assertEqual(f['buyerName'],'')
        self.assertEqual(f['sellerName'],'')

    def test_travel_and_expiry_dates_do_not_fill_missing_invoice_date(self):
        for title in ['电子发票（铁路电子客票）','电子发票（航空运输电子客票行程单）']:
            f=extract_fields(title+'\n日期：2026年11月17日\n有效截止日期：2027年11月13日\nFlight Date:2026-11-17')
            self.assertEqual(f['date'],'')

    def test_ocr_title_noise_can_use_distinct_aviation_labels(self):
        r=_result('电子发票\n（航宇运输申客票行程单）\n航班号\n民航发展基金\n电子客票号码：\n合计：CNY670.00')
        self.assertEqual(r['suggestedCategory'],'flight')
        self.assertEqual(r['fields']['amount'],670)

    def test_generic_invoice_does_not_treat_any_fare_or_single_total_as_gross(self):
        f=extract_fields('普通电子发票\n票价：550.46\n合计：100.00')
        self.assertEqual(f['amount'],'')
        self.assertEqual(_result('车费凭证')['suggestedCategory'],'')

    def test_rail_geometry_supports_a_split_label_and_price(self):
        blocks=[dict(text='票价',x=.1,y=.4,width=.05,height=.03,page=0),dict(text='￥493.00',x=.17,y=.4,width=.12,height=.03,page=0)]
        self.assertEqual(extract_fields('电子发票（铁路电子客票）\n票价\n￥493.00',blocks)['amount'],493)

if __name__=='__main__': unittest.main()
