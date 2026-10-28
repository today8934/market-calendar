"""Fetch public calendars daily. Missing sources never erase existing events."""
import argparse
import calendar
import copy
import datetime as dt
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar

ROOT = Path(__file__).resolve().parent
KST = ZoneInfo('Asia/Seoul')
NY = 'America/New_York'
NOW = dt.datetime.now(KST).replace(microsecond=0)
TODAY = NOW.date()
START = TODAY - dt.timedelta(days=TODAY.weekday())
END = TODAY + dt.timedelta(days=90)
STAMP = NOW.isoformat()
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; MarketCalendar/1.0)', 'Accept': 'application/json,text/html,text/calendar,*/*'}
KINDBASE = 'https://kind.krx.co.kr/corpgeneral/irschedule.do'

def get(url, params=None, post=False):
    response = requests.request('POST' if post else 'GET', url,
        **({'data': params} if post else {'params': params}), headers=HEADERS, timeout=(10, 35))
    response.raise_for_status()
    return response

def soup(response):
    return BeautifulSoup(response.content, 'html.parser', from_encoding='utf-8')

def iso(date, clock, zone):
    return dt.datetime.fromisoformat(f'{date}T{clock}').replace(tzinfo=ZoneInfo(zone)).isoformat()

def event(eid, title, market, category, date, clock, zone, name, url, official=True, tickers=None, details='', **extra):
    return dict(id=eid,title=title,market=market,category=category,date=str(date),
        startsAt=iso(str(date),clock,zone) if clock else None,sourceZone=zone,
        status='confirmed' if official else 'tentative',importance='high' if category=='rates' else 'normal',
        tickers=tickers or [],source=dict(name=name,url=url,official=official),
        details=details or title,checkedAt=STAMP,**extra)

def within(date):
    return START <= dt.date.fromisoformat(str(date)) <= END

def bea():
    url='https://www.bea.gov/news/schedule'
    doc=soup(get(url)); out=[]
    for table in doc.select('table'):
        match=re.search(r'Year\s+(20\d{2})',table.get_text(' ',strip=True))
        if not match: continue
        year=int(match[1])
        for row in table.select('tr'):
            date=row.select_one('.release-date'); title=row.select_one('.release-title'); clock=row.select_one('small')
            if not all([date,title,clock]): continue
            original=title.get_text(' ',strip=True)
            label=next((ko for en,ko in [('GDP (Advance','미국 GDP 속보치'),('GDP (Second','미국 GDP 수정치'),('GDP (Third','미국 GDP 확정치'),('Personal Income and Outlays','미국 PCE 물가·개인소득·소비'),('U.S. International Trade','미국 무역수지')] if original.startswith(en)),None)
            if not label: continue
            d=dt.datetime.strptime(f'{date.get_text(strip=True)} {year}','%B %d %Y').date()
            if not within(d):continue
            t=dt.datetime.strptime(clock.get_text(strip=True),'%I:%M %p').strftime('%H:%M')
            eid='bea-'+hashlib.sha256(original.encode()).hexdigest()[:18]
            out.append(event(eid,label,'US','economy',d,t,NY,'미국 BEA',url,details=original))
    if not out: raise ValueError('BEA 일정 행을 확인하지 못했습니다.')
    return out,[]

def bok():
    out=[]
    for year in range(START.year,END.year+1):
        url=f'https://www.bok.or.kr/portal/stats/statsPublictSchdul/listCldr.do?menuNo=200775&year={year}'
        doc=soup(get(url))
        for row in doc.select('tr'):
            cells=row.select('td')
            if len(cells)<3:continue
            d=cells[0].get_text(strip=True); t=cells[1].get_text(strip=True); title=cells[2].get_text(' ',strip=True)
            if not re.fullmatch(r'20\d{2}-\d{2}-\d{2}',d) or not within(d):continue
            anchor=cells[2].select_one('a[onclick]'); match=re.search(r"schdulPop\('([0-9]+)'",anchor.get('onclick','') if anchor else '')
            if not match:continue
            t=':'.join(p.zfill(2) for p in t.split(':')) if re.fullmatch(r'\d{1,2}:\d{2}',t) else None
            out.append(event('bok-stat-'+match[1],title,'KR','economy',d,t,'Asia/Seoul','한국은행 통계공표일정',url))
    if not out:raise ValueError('한국은행 통계 일정을 확인하지 못했습니다.')
    return out,[]

def fed():
    url='https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
    doc=soup(get(url)); out=[]
    for panel in doc.select('.panel'):
        heading=panel.select_one('.panel-heading'); match=re.search(r'(20\d{2}) FOMC Meetings',heading.get_text() if heading else '')
        if not match:continue
        year=int(match[1])
        for row in panel.select('.fomc-meeting'):
            month=row.select_one('.fomc-meeting__month'); dates=row.select_one('.fomc-meeting__date')
            if not month or not dates:continue
            name=month.get_text(strip=True).split('/')[-1]; numbers=re.findall(r'\d+',dates.get_text())
            if not numbers:continue
            try:d=dt.date(year,list(calendar.month_name).index(name),int(numbers[-1]))
            except ValueError:continue
            if within(d):out.append(event(f'fed-rate-{d:%Y-%m}','미국 FOMC 정책 결정일','US','rates',d,None,NY,'미국 연방준비제도',url,details='정례회의 마지막 날. 정확한 발표 시각은 연준 월간 일정에서 추가 확인합니다.'))
    # Only attach exact times when present in the monthly calendar, never from a convention.
    for e in list(out):
        d=dt.date.fromisoformat(e['date']); monthurl=f'https://www.federalreserve.gov/newsevents/{d.year}-{calendar.month_name[d.month].lower()}.htm'
        try:
            monthly=soup(get(monthurl))
            for p in monthly.find_all('p'):
                label=p.get_text(' ',strip=True)
                if label not in ('FOMC Meeting','FOMC Press Conference'):continue
                block=p.parent
                for _ in range(4):
                    if block is None:break
                    txt=block.get_text(' ',strip=True)
                    clock=re.search(r'(\d{1,2}):(\d{2})\s*(a\.m\.|p\.m\.)',txt)
                    # Date lives in the same row. Require the final standalone day.
                    if clock and re.search(r'\b'+str(d.day)+r'\s*$',txt):
                        hour=int(clock[1])%12+(12 if clock[3]=='p.m.' else 0); t=f'{hour:02}:{clock[2]}'
                        if label=='FOMC Meeting':e.update(startsAt=iso(e['date'],t,NY),source=dict(name='연준 월간 캘린더',url=monthurl,official=True))
                        else:out.append(event(f'fed-press-{d:%Y-%m}','연준 의장 기자회견','US','rates',d,t,NY,'연준 월간 캘린더',monthurl))
                        break
                    block=block.parent
        except requests.RequestException:pass
    if not out:raise ValueError('다가오는 FOMC 회의를 확인하지 못했습니다.')
    return out,[]

def bls():
    url='https://www.bls.gov/schedule/news_release/bls.ics'
    cal=Calendar.from_ical(get(url).content);out=[]
    names={'Employment Situation':'미국 고용보고서','Consumer Price Index':'미국 소비자물가 CPI','Producer Price Index':'미국 생산자물가 PPI','Job Openings and Labor Turnover':'미국 구인·이직 JOLTS','Employment Cost Index':'미국 고용비용지수','Productivity and Costs':'미국 생산성·단위노동비용','U.S. Import and Export Price Indexes':'미국 수출입물가'}
    for item in cal.walk('VEVENT'):
        title=str(item.get('SUMMARY',''));name=next((ko for en,ko in names.items() if title.startswith(en)),None)
        if not name:continue
        start=item.decoded('DTSTART')
        if not isinstance(start,dt.datetime) or not start.tzinfo:raise ValueError('BLS 시각에 시간대가 없습니다.')
        start=start.astimezone(ZoneInfo(NY)); d=start.date()
        if not within(d):continue
        uid=str(item.get('UID') or title)
        out.append(event('bls-feed-'+hashlib.sha256(uid.encode()).hexdigest()[:18],name,'US','economy',d,start.strftime('%H:%M'),NY,'미국 BLS iCalendar',url,details=title,providerKey=uid))
    if not out:raise ValueError('BLS에서 대상 기간의 일정을 확인하지 못했습니다.')
    return out,[]

def kind():
    out=[];catalog={}; seen=set(); seen_pages=set()
    for page in range(1,21):
        params=dict(method='searchIRScheduleSub',forward='searchirschedule_sub',fromDate=START.isoformat(),toDate=END.isoformat(),currentPageSize='100',pageIndex=str(page),orderMode='4',orderStat='D',marketType='')
        doc=soup(get(KINDBASE,params,post=True)); rows=doc.select('table.list tbody tr')
        fingerprint=hashlib.sha256(str(rows).encode()).hexdigest()
        if fingerprint in seen_pages:raise ValueError('KIND 페이지가 반복되어 수집을 중단했습니다.')
        seen_pages.add(fingerprint)
        for row in rows:
            cells=row.select('td')
            if len(cells)<6:continue
            company=cells[1].select_one('a'); link=cells[2].select_one('a')
            if not company or not link:continue
            title=link.get('title') or link.get_text(' ',strip=True)
            if not re.search(r'실적\s*(?:발표|공시)|earnings',title,re.I) or re.search(r'\bNDR\b|Non.Deal|로드쇼',title,re.I):continue
            # KRX issuer code omits the ordinary-share suffix 0.
            code=re.search(r"companysummary_open\('([0-9]{5,6})'",company.get('onclick','')); seq=re.search(r"fnDetailView\('([0-9]+)'",link.get('onclick',''))
            if not code or not seq:continue
            symbol=code[1]+'0' if len(code[1])==5 else code[1]; companyname=company.get('title') or company.get_text(strip=True)
            d=cells[4].get_text(strip=True); t=cells[5].get_text(strip=True)
            if not re.fullmatch(r'\d{2}:\d{2}',t):t=None
            if not within(d):continue
            period=re.search(r'(20\d{2})\s*년?\s*([1-4])\s*분기',title) or re.search(r'(20\d{2})\s*Q([1-4])',title,re.I)
            if not period:
                rev=re.search(r'([1-4])Q\s*(20\d{2})',title,re.I)
                period=(None,rev[2],rev[1]) if rev else None
            eid=f'kr-{symbol}-{period[1]}-q{period[2]}-call' if period else 'kind-ir-'+seq[1]
            if eid in seen:continue
            seen.add(eid)
            url=KINDBASE+'?method=searchIRSchedulePopup&irSeq='+seq[1]
            out.append(event(eid,companyname+' 실적 설명회','KR','earnings',d,t,'Asia/Seoul','한국거래소 KIND IR 일정',url,tickers=['KR:'+symbol],details=title+' · KIND에 등록된 IR 행사이며 실적 자료 공개 시각과 다를 수 있습니다.',providerKey='kind:'+seq[1]))
            catalog[symbol]=dict(market='KR',symbol=symbol,name=companyname)
        total=re.search(r'전체\s*([\d,]+)\s*건',doc.get_text(' ',strip=True))
        if not total:raise ValueError('KIND 전체 건수 확인 실패')
        if page*100>=int(total[1].replace(',','')):break
    else:raise ValueError('KIND 페이지 수가 수집 한도를 넘었습니다.')
    return out,list(catalog.values())

def nasdaq_day(d, known):
    url='https://api.nasdaq.com/api/calendar/earnings?date='+d.isoformat()
    payload=get(url).json();data=payload.get('data')
    if not isinstance(data,dict) or 'rows' not in data:raise ValueError(f'Nasdaq {d} 응답 확인 실패')
    rows=data['rows'] or [];out=[];catalog=[]
    for row in rows:
        symbol=row.get('symbol','');name=row.get('name',symbol)
        if not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,11}',symbol):continue
        cap=re.sub(r'[^\d.]','',row.get('marketCap') or '')
        if symbol not in known and (not cap or float(cap)<10_000_000_000):continue
        period=row.get('fiscalQuarterEnding','')
        if not re.fullmatch(r'[A-Za-z]{3}/20\d{2}',period):continue
        ending=dt.datetime.strptime(period,'%b/%Y').strftime('%Y-%m')
        session={'time-after-hours':'after-close','time-pre-market':'before-open'}.get(row.get('time'))
        extra={'session':session} if session else {}
        slug=symbol.lower().replace('.','-')
        out.append(event(f'us-{slug}-{ending}-release',name+' 실적 발표','US','earnings',d,None,NY,'Nasdaq 실적 캘린더',url,False,['US:'+symbol],details=f'회계기간 종료 {period}. Nasdaq 집계 일정은 예상치가 포함되어 기업 IR 재확인이 필요합니다. 정확한 공개 시각은 미정입니다.',**extra))
        catalog.append(dict(market='US',symbol=symbol,name=name))
    return out,catalog

def nasdaq(known):
    # Bounded rolling window; longer-dated curated events are preserved.
    days=[START+dt.timedelta(days=i) for i in range((TODAY+dt.timedelta(days=30)-START).days+1) if (START+dt.timedelta(days=i)).weekday()<5]
    out=[];catalog=[];errors=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[(d,pool.submit(nasdaq_day,d,known)) for d in days]
        for d,future in futures:
            try:e,c=future.result();out.extend(e);catalog.extend(c)
            except Exception as exc:errors.append(f'{d}: {exc}')
    if errors:raise ValueError('Nasdaq 일부 날짜 수집 실패: '+'; '.join(errors[:3]))
    return out,catalog

def samsung():
    url='https://www.samsung.com/global/ir/ir-events-presentations/events/'
    text=soup(get(url)).get_text(' ',strip=True);out=[]
    pattern=r'([1-4])Q(\d{2}) (Earnings Conference Call|Guidance Announcement)\s+([A-Z][a-z]+ \d{1,2}, 20\d{2})(?:,\s*(\d{1,2}:\d{2})\s*(a\.m\.|p\.m\.)\s*KST)?'
    for q,yy,kind_,date,clock,ampm in re.findall(pattern,text):
        d=dt.datetime.strptime(date,'%B %d, %Y').date()
        if not within(d):continue
        if clock:clock=dt.datetime.strptime(clock+' '+ampm.replace('.','').upper(),'%I:%M %p').strftime('%H:%M')
        call=kind_.startswith('Earnings')
        out.append(event(f'samsung-20{yy}-q{q}-'+('call' if call else 'guidance'),'삼성전자 Q'+q+(' 실적 콘퍼런스콜' if call else ' 잠정실적'),'KR','earnings',d,clock or None,'Asia/Seoul','삼성전자 IR',url,tickers=['KR:005930']))
    if not out:raise ValueError('삼성전자 대상 기간 IR 일정 없음. 기존 정보 유지.')
    return out,[]

def validate(data):
    seen=set()
    for e in data['events']:
        if e['id'] in seen:raise ValueError('중복 ID: '+e['id'])
        seen.add(e['id']);dt.date.fromisoformat(e['date']);zone=ZoneInfo(e['sourceZone'])
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,119}',e['id']):raise ValueError('잘못된 ID')
        if e['market'] not in ('KR','US','EU','JP','CA','NZ','AU','CN') or e['category'] not in ('rates','economy','earnings','holiday'):raise ValueError('시장/종류 오류')
        if e['status'] not in ('confirmed','tentative','cancelled'):raise ValueError('상태 오류')
        if any(not re.fullmatch(r'(KR:\d{6}|US:[A-Z][A-Z0-9.-]{0,11})',t) for t in e['tickers']):raise ValueError('종목코드 오류')
        if e.get('startsAt'):
            start=dt.datetime.fromisoformat(e['startsAt'])
            if start.tzinfo is None or start.astimezone(zone).date().isoformat()!=e['date'] or start.utcoffset()!=start.astimezone(zone).utcoffset():raise ValueError('시간대 오류: '+e['id'])
        if e['status']=='confirmed' and not e['source']['official']:raise ValueError('비공식 일정의 확정 표시')
        if e['category']=='earnings' and not e['tickers']:raise ValueError('실적 종목 누락')
        if not e['source']['url'].startswith('https://'):raise ValueError('출처 링크 오류')

def merge(data, incoming):
    existing={e['id']:e for e in data['events']};added=changed=0
    for e in incoming:
        old=existing.get(e['id'])
        if old is None:
            # Keep the previously published stable ID when the same event arrives from a new feed.
            matches=[x for x in existing.values() if x['market']==e['market'] and x['category']==e['category'] and x['tickers']==e['tickers'] and x['date']==e['date'] and ((x['title']==e['title']) or (e['category']=='earnings' and (('call' in x['id'])==('call' in e['id']))))]
            if len(matches)==1:old=matches[0];e['id']=old['id']
        if old:
            # An estimate cannot overwrite an officially confirmed release or call.
            if old['source']['official'] and not e['source']['official']:continue
            # A date-only feed cannot discard an exact official time already known for the same day.
            if old.get('startsAt') and not e.get('startsAt') and old['date']==e['date']:
                eid=e['id'];e=copy.deepcopy(old);e['id']=eid;e['dateCheckedAt']=STAMP
            e['history']=copy.deepcopy(old.get('history',[]))
            if old.get('previousDate'):e['previousDate']=old['previousDate']
            if any(old.get(k)!=e.get(k) for k in ('date','startsAt','status','session')):
                e['history'].append({k:old.get(k) for k in ('date','startsAt','status')}|{'changedAt':STAMP});e['previousDate']=old['date'];changed+=1
        else:added+=1
        existing[e['id']]=e
    data['events']=sorted(existing.values(),key=lambda e:(e['date'],e.get('startsAt') or '',e['id']))
    return added,changed

def write(data):
    validate(data)
    html=(ROOT/'index.html').read_text(encoding='utf-8-sig')
    payload=json.dumps(data,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    html,count=re.subn(r'(<script id="calendar-data" type="application/json">).*?(</script>)',lambda m:m[1]+payload+m[2],html,flags=re.S)
    if count!=1:raise ValueError('HTML 데이터 영역 오류')
    (ROOT/'events.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (ROOT/'index.html').write_text(html,encoding='utf-8')

def run():
    data=json.loads((ROOT/'events.json').read_text(encoding='utf-8-sig'));known={s['symbol'] for s in data['catalog'] if s['market']=='US'}
    feeds=[('BEA 경제지표',bea),('한국은행 통계',bok),('FOMC',fed),('BLS 경제지표',bls),('KIND 기업실적',kind),('Nasdaq 기업실적',lambda:nasdaq(known)),('삼성전자 IR',samsung)]
    incoming=[];catalog={s['market']+':'+s['symbol']:s for s in data['catalog']};report=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[(name,pool.submit(fn)) for name,fn in feeds]
        for name,future in futures:
            try:
                ev,stocks=future.result();incoming.extend(ev)
                for stock in stocks:catalog.setdefault(stock['market']+':'+stock['symbol'],stock)
                report.append(dict(name=name,status='ok',events=len(ev),checkedAt=STAMP))
            except Exception as exc:report.append(dict(name=name,status='error',message=str(exc)[:250]))
    for e in incoming:
        if e['id'].startswith('us-') and e['tickers'][0] in catalog:e['title']=catalog[e['tickers'][0]]['name']+' 실적 발표'
    added,changed=merge(data,incoming);data['catalog']=list(catalog.values())
    data.update(updatedAt=STAMP,revision=NOW.strftime('%Y%m%dT%H%M%S')+'-daily')
    data['coverage'].update(to=max(data['coverage']['to'],END.isoformat()),note='한국·미국 주요 일정과 일부 세계 중앙은행 일정. KIND 실적 행사와 Nasdaq 시가총액 100억 달러 이상 또는 등록 기업을 수집합니다. Nasdaq 집계는 예상 일정으로 표시합니다. 정확한 시각 미정은 현지일, 미국 장후는 다음 한국 날짜의 새벽 시간대로 표시합니다. 해외 중앙은행 일부·휴장은 별도 확인 자료입니다. 출처별 수집 상태와 각 일정의 확인 시각을 확인하세요.')
    data['automation']=dict(schedule='매일 오전 7시 KST',engine='GitHub Actions',lastRunAt=STAMP,status='partial' if any(r['status']=='error' for r in report) else 'ok',sources=report,added=added,changed=changed)
    write(data);print(json.dumps(data['automation'],ensure_ascii=False))
    if all(r['status']=='error' for r in report):raise SystemExit('모든 출처 수집 실패. 기존 이벤트는 보존했습니다.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--validate',action='store_true');args=parser.parse_args()
    if args.validate:validate(json.loads((ROOT/'events.json').read_text(encoding='utf-8-sig')));print('valid')
    else:run()
