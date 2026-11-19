"""Publish only the new paper-policy metadata while the full cache warms."""
import json
import daily_spread_policy as S
import publisher as P


def main():
    import datetime
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).date()
    if str(today) >= S.ACTIVATION:
        raise RuntimeError('Metadata-only bootstrap is allowed only before activation')
    data = S.payload()
    assert data['candidate'] == 'trend' and data['paper_only']
    P.write('daytrade/NIFTY/paper_policy',data)
    actual = P.db().reference('/live/daytrade/NIFTY/paper_policy').get()
    assert actual['version'] == S.VERSION and actual['candidate'] == S.ACTIVE
    print(json.dumps(actual,indent=2))


if __name__ == '__main__':
    main()
