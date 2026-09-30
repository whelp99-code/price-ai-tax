"""Read explicitly printed query periods; never infer completeness from row dates."""
import re
from datetime import date


def printed_query_period(text):
    pattern=r'(?:조회\s*기간|조회\s*범위)\s*[:：]\s*(\d{4}[-./]\d{1,2}[-./]\d{1,2})\s*(?:~|～|부터|–|—)\s*(\d{4}[-./]\d{1,2}[-./]\d{1,2})'
    pairs=re.findall(pattern,text)
    if not pairs:return {}
    ranges=[]
    try:
        for first,last in pairs:
            parse=lambda s:date(*map(int,re.split(r'[-./]',s)))
            start,end=parse(first),parse(last)
            if start>end:raise ValueError('reverse interval')
            ranges.append((start.isoformat(),end.isoformat()))
    except ValueError:return {'declared_period_invalid':True}
    if len(set(ranges))!=1:return {'declared_period_conflict':True}
    return {'declared_period':{'start':ranges[0][0],'end':ranges[0][1],'basis':'printed_query_period_not_provider_verified'}}


def is_no_activity_line(row):
    nonempty=[str(v).strip() for v in row if str(v).strip()]
    if len(nonempty)!=1:return False
    normalized=re.sub(r'[\s.!。]','',nonempty[0])
    return normalized in {'조회된거래내역이없습니다','거래내역이없습니다','거래내역없음','조회결과0건'}
