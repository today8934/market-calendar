import copy
import datetime as dt
import unittest
from zoneinfo import ZoneInfo
import update_calendar as c

class CalendarTests(unittest.TestCase):
    def sample(self):
        return c.event('fed-rate-2026-10','FOMC','US','rates','2026-10-28','14:00',c.NY,'Fed','https://www.federalreserve.gov/')

    def test_dst_korean_dates(self):
        self.assertEqual(dt.datetime.fromisoformat(c.iso('2026-10-28','14:00',c.NY)).astimezone(c.KST).isoformat(),'2026-10-29T03:00:00+09:00')
        self.assertEqual(dt.datetime.fromisoformat(c.iso('2026-12-09','14:00',c.NY)).astimezone(c.KST).isoformat(),'2026-12-10T04:00:00+09:00')

    def test_date_change_keeps_identity_history_and_other_events(self):
        old=self.sample();other=copy.deepcopy(old);other['id']='other-event'
        data={'events':[old,other]};new=copy.deepcopy(old);new['date']='2026-10-29';new['startsAt']=c.iso(new['date'],'14:00',c.NY)
        c.merge(data,[new]);self.assertEqual(len(data['events']),2)
        updated=next(x for x in data['events'] if x['id']==old['id'])
        self.assertEqual(updated['history'][0]['date'],'2026-10-28')

    def test_estimate_cannot_replace_official(self):
        old=self.sample();data={'events':[old]};new=copy.deepcopy(old);new['source']['official']=False;new['status']='tentative';new['date']='2026-10-30'
        c.merge(data,[new]);self.assertEqual(data['events'][0]['date'],'2026-10-28')

    def test_missing_feed_keeps_all_events(self):
        old=self.sample();data={'events':[old]};c.merge(data,[]);self.assertEqual(data['events'],[old])

    def test_wrong_dst_offset_rejected(self):
        e=self.sample();e['startsAt']='2026-10-28T14:00:00-05:00'
        with self.assertRaises(ValueError):c.validate({'events':[e]})

if __name__=='__main__':unittest.main()
