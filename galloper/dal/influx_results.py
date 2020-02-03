import numpy as np
from influxdb import InfluxDBClient
from datetime import datetime, timezone
from galloper.constants import str_to_timestamp

influx_client = None


def get_client():
    global influx_client
    if not influx_client:
        influx_client = InfluxDBClient("carrier-influx", 8086, '', '')
    return influx_client


def get_test_details(build_id, test_name, lg_type):
    test = {
        "start_time": 0,
        "name": test_name,
        "environment": "",
        "type": "",
        "end_time": 0,
        "failures": 0,
        "total": 0,
        "thresholds_missed": 0,
        "throughput": 0,
        "vusers": 0,
        "pct95": 0,
        "duration": 0,
        "1xx": 0,
        "2xx": 0,
        "3xx": 0,
        "4xx": 0,
        "5xx": 0,
        "build_id": build_id,
        "lg_type": lg_type,
        "requests": []
    }
    q_start_time = f"select time, active from {lg_type}..\"users\" " \
                   f"where build_id='{build_id}' order by time asc limit 1"
    q_end_time = f"select time, active from {lg_type}..\"users\" " \
                 f"where build_id='{build_id}' order by time desc limit 1"
    q_response_codes = f"select sum(\"1xx\") as \"1xx\", sum(\"2xx\") as \"2xx\", sum(\"3xx\") as \"3xx\", " \
                       f"sum(\"4xx\") as \"4xx\", sum(\"5xx\") as \"5xx\", sum(\"ko\") as KO, " \
                       f"sum(\"total\") as Total, sum(throughput) as \"throughput\" " \
                       f"from comparison..api_comparison where build_id='{build_id}'"
    q_total_users = f"show tag values on comparison with key=\"users\" where build_id='{build_id}'"
    q_env = f"show tag values on comparison with key=\"env\" where build_id='{build_id}'"
    q_type = f"show tag values on comparison with key=\"test_type\" where build_id='{build_id}'"
    q_pct95 = f"select percentile(response_time, 95) from {lg_type}..{test_name} " \
              f"where build_id='{build_id}' and status='OK'"
    q_requests_name = f"show tag values on comparison with key=\"request_name\" " \
                      f"where build_id='{build_id}'"
    client = get_client()
    test["start_time"] = list(client.query(q_start_time)["users"])[0]["time"]
    test["end_time"] = list(client.query(q_end_time)["users"])[0]["time"]
    test["duration"] = round(str_to_timestamp(test["end_time"]) - str_to_timestamp(test["start_time"]), 1)
    test["vusers"] = list(client.query(q_total_users)["api_comparison"])[0]["value"]
    test["environment"] = list(client.query(q_env)["api_comparison"])[0]["value"]
    test["type"] = list(client.query(q_type)["api_comparison"])[0]["value"]
    test["pct95"] = list(client.query(q_pct95)[test_name])[0]["percentile"]
    test["requests"] = [name["value"] for name in client.query(q_requests_name)["api_comparison"]]
    response_data = list(client.query(q_response_codes)['api_comparison'])[0]
    test['total'] = response_data['Total']
    test['failures'] = response_data['KO']
    test['throughput'] = round(response_data['throughput'], 1)
    test['1xx'] = response_data['1xx']
    test['2xx'] = response_data['2xx']
    test['3xx'] = response_data['3xx']
    test['4xx'] = response_data['4xx']
    test['5xx'] = response_data['5xx']
    return test


def get_backend_users(build_id, lg_type, start_time, end_time, aggregation):
    query = f"select time, sum(active) from {lg_type}..\"users\" where " \
            f"time>='{start_time}' and time<='{end_time}' and build_id='{build_id}' " \
            f"group by time(1s)"
    client = get_client()
    res = client.query(query)['users']
    timestamps = []
    results = {"users": {}}
    # aggregation of users
    _tmp = []
    for _ in res:
        _tmp.append(_['sum'] if _['sum'] else 0)
        results["users"][_['time']] = None
        if _['time'] not in timestamps:
            timestamps.append(_['time'])
        if (len(_tmp) % int(aggregation.replace('s', ''))) == 0:
            results["users"][_['time']] = int(np.percentile(np.array(_tmp), 50, interpolation='lower'))
            _tmp = []
    return timestamps, results


def get_backend_requests(build_id, test_name, lg_type, start_time, end_time, aggregation,
                         timestamps=None, users=None, scope=None, aggr='pct95'):
    """
    :param build_id: - could be obtained from control_tower during tests execution
    :param test_name: - name of the test used as measurement in database
    :param lg_type: - either jmeter or gatling as a DB name
    :param start_time
    :param end_time
    :return:

    """
    scope_addon = ""
    group_by = ""
    if aggr in ["Min", "Max"]:
        aggr_func = f"{aggr.lower()}(response_time)"
    elif 'pct' in aggr:
        aggr = aggr.replace('pct', '')
        aggr_func = f"percentile(response_time, {aggr})"
    else:
        aggr_func = f"percentile(response_time, 50)"

    if scope and scope != 'All':
        scope_addon = f"and request_name='{scope}'"
    elif scope != 'All':
        group_by = "request_name, "

    if not (timestamps and users):
        timestamps, users = get_backend_users(build_id, lg_type, start_time, end_time, aggregation)
    query = f"select time, {group_by}{aggr_func} as rt from {lg_type}..{test_name} " \
            f"where time>='{start_time}' and time<='{end_time}' and " \
            f"build_id='{build_id}' {scope_addon} group by {group_by}time({aggregation})"
    res = get_client().query(query)[test_name]
    results = {}
    if group_by:
        for _ in res:
            if not _.get('request_name'):
                continue
            if _['request_name'] not in results:
                results[_['request_name']] = {}
                for ts in timestamps:
                    results[_['request_name']][ts] = None
            results[_['request_name']][_['time']] = _['rt']
    else:
        results['response'] = {}
        for ts in timestamps:
            results['response'][ts] = None
        for _ in res:
            results['response'][_['time']] = _['rt']
    return timestamps, results, users


def get_tps(build_id, test_name, lg_type, start_time, end_time, aggregation, timestamps=None, users=None, scope=None):
    if not (timestamps and users):
        timestamps, users = get_backend_users(build_id, lg_type, start_time, end_time, aggregation)
    scope_addon = ""
    if scope and scope != 'All':
        scope_addon = f"and request_name='{scope}'"
    responses_query = f"select time, count(response_time) from {lg_type}..{test_name} where time>='{start_time}' " \
                      f"and time<='{end_time}' and build_id='{build_id}' {scope_addon} group by time(1s)"
    res = get_client().query(responses_query)[test_name]
    results = {"responses": {}}
    for _ in timestamps:
        results['responses'][_] = None
    # aggregation of responses
    _tmp = []
    for _ in res:
        _tmp.append(_['count'])
        if (len(_tmp) % int(aggregation.replace('s', ''))) == 0:
            results['responses'][_['time']] = float(sum(_tmp)) / int(aggregation.replace('s', ''))
            _tmp = []
    return timestamps, results, users


def get_response_codes(build_id, test_name, lg_type, start_time, end_time, aggregation, timestamps=None,
                       users=None, scope=None, aggr="2xx"):
    if not (timestamps and users):
        timestamps, users = get_backend_users(build_id, lg_type, start_time, end_time, aggregation)
    scope_addon = f"and status_code=~/^{aggr[0]}/ "
    if scope and scope != 'All':
        scope_addon = f"and request_name='{scope}'"
    rcode_query = f"select time, count(status_code) from {lg_type}..{test_name} where build_id='{build_id}' " \
                  f"and time>='{start_time}' and time<='{end_time}' {scope_addon} group by time({aggregation})"
    res = get_client().query(rcode_query)[test_name]
    results = {"rcodes": {}}
    for _ in timestamps:
        results['rcodes'][_] = None
    for _ in res:
        results['rcodes'][_['time']] = _["count"]
    return timestamps, results, users


def get_errors(build_id, test_name, lg_type, start_time, end_time, aggregation, timestamps=None, users=None, scope=None):
    if not (timestamps and users):
        timestamps, users = get_backend_users(build_id, lg_type, start_time, end_time, aggregation)
    scope_addon = ""
    if scope and scope != 'All':
        scope_addon = f"and request_name='{scope}'"
    error_query = f"select time, count(status) from {lg_type}..{test_name} " \
                  f"where time>='{start_time}' and time<='{end_time}' and build_id='{build_id}' " \
                  f"and status='KO' {scope_addon} group by time(1s)"
    results = {"errors": {}}
    for _ in timestamps:
        results['errors'][_] = None
    res = get_client().query(error_query)[test_name]
    _tmp = []
    for _ in res:
        _tmp.append(_['count'])
        if (len(_tmp) % int(aggregation.replace('s', ''))) == 0:
            results['errors'][_['time']] = sum(_tmp)
            _tmp = []
    return timestamps, results, users


def get_hits(build_id, test_name, lg_type, start_time, end_time, aggregation, timestamps=None, users=None, scope=None):
    if not (timestamps and users):
        timestamps, users = get_backend_users(build_id, lg_type, start_time, end_time, aggregation)
    scope_addon = ""
    if scope and scope != 'All':
        scope_addon = f"and request_name='{scope}'"
    hits_query = f"select time, response_time from {lg_type}..{test_name} where " \
                 f"time>='{start_time}' and time<='{end_time}' and build_id='{build_id}' {scope_addon}"
    results = {"hits": {}}
    for _ in timestamps:
        results['hits'][_] = 0
    res = get_client().query(hits_query)[test_name]
    for _ in res:
        timestamp = str_to_timestamp(_['time'])
        hit_time = datetime.fromtimestamp(timestamp - float(_["response_time"]) / 1000.0, tz=timezone.utc)
        if hit_time.strftime("%Y-%m-%dT%H:%M:%SZ") in results['hits']:
            results['hits'][hit_time.strftime("%Y-%m-%dT%H:%M:%SZ")] += 1
    # aggregation of hits
    _tmp = []
    _ts = None
    for _ in results['hits']:
        if len(_tmp) == 0:
            _ts = _
        _tmp.append(results['hits'][_])
        results['hits'][_] = None
        if (len(_tmp) % int(aggregation.replace('s', ''))) == 0:
            results['hits'][_ts] = float(sum(_tmp)) / int(aggregation.replace('s', ''))
            _tmp = []
            _ts = None
    return timestamps, results, users


def get_hits_tps(build_id, test_name, lg_type, start_time, end_time, aggregation):
    timestamps, users = get_backend_users(build_id, lg_type, start_time, end_time, aggregation)
    results = {"responses": {}, "hits": {}}
    _, responses, _ = get_tps(build_id, test_name, lg_type, start_time, end_time, aggregation, timestamps, users)
    results['responses'] = responses['responses']
    _, hits, _ = get_hits(build_id, test_name, lg_type, start_time, end_time, aggregation, timestamps, users)
    results['hits'] = hits['hits']
    return timestamps, results, users


def average_responses(build_id, test_name, lg_type, start_time, end_time, aggregation):
    timestamps, users = get_backend_users(build_id, lg_type, start_time, end_time, aggregation)
    responses_query = f"select time, percentile(response_time, 95) from {lg_type}..{test_name} " \
                      f"where time>='{start_time}' " \
                      f"and time<='{end_time}' and build_id='{build_id}' group by time({aggregation})"
    res = get_client().query(responses_query)[test_name]
    results = {"responses": {}}
    for _ in timestamps:
        results['responses'][_] = None
    for _ in res:
        results["responses"][_['time']] = _['percentile']
    return timestamps, results, users


def get_build_data(build_id, test_name, lg_type, start_time, end_time):
    requests_in_range = f"select time, request_name, max(response_time) from {lg_type}..{test_name} " \
                        f"where time>='{start_time}' " \
                        f"and time<='{end_time}' and build_id='{build_id}' group by request_name"
    res = get_client().query(requests_in_range)[test_name]
    requests = "|".join([f"'{each['request_name']}'" for each in res])
    query = f"select * from comparison..api_comparison where build_id='{build_id}' and request_name=~/^[{requests}]/"
    return list(get_client().query(query)['api_comparison'])