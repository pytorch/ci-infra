"""delete_dns_record must delete what actually exists (CNAME since Oct 2025), not a
reconstructed A record — the mismatch leaked records until the zone hit 10k."""

from unittest.mock import MagicMock

import pytest

from shared import dns_utils

ALB = "pytorch-gpu-dev-jupyter-alb-1.us-east-2.elb.amazonaws.com"


def _rrs(name, rtype, value):
    return {"Name": name, "Type": rtype, "TTL": 60, "ResourceRecords": [{"Value": value}]}


@pytest.fixture
def route53(monkeypatch):
    zone = [
        _rrs("dark_goldfish-2.devservers.io.", "CNAME", ALB),
        _rrs("_port.dark_goldfish-2.devservers.io.", "TXT", '"0"'),
        _rrs("dark_goldfish-3.devservers.io.", "CNAME", ALB),  # neighbour, must survive
    ]
    client = MagicMock()

    def list_rrs(HostedZoneId, StartRecordName, MaxItems):
        names = sorted(r["Name"] for r in zone)
        start = next((i for i, n in enumerate(names) if n >= StartRecordName), len(names))
        keep = set(names[start:start + int(MaxItems)])
        return {"ResourceRecordSets": [r for r in zone if r["Name"] in keep]}

    client.list_resource_record_sets.side_effect = list_rrs
    client.change_resource_record_sets.return_value = {"ChangeInfo": {"Id": "c-1"}}
    monkeypatch.setattr(dns_utils, "route53_client", client)
    monkeypatch.setattr(dns_utils, "DOMAIN_NAME", "devservers.io")
    monkeypatch.setattr(dns_utils, "HOSTED_ZONE_ID", "Z123")
    return client


def test_deletes_existing_cname_and_txt_verbatim(route53):
    assert dns_utils.delete_dns_record("dark_goldfish-2", "3.148.180.205", 32045)

    changes = route53.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"]
    assert [c["Action"] for c in changes] == ["DELETE", "DELETE"]
    assert [c["ResourceRecordSet"] for c in changes] == [
        _rrs("dark_goldfish-2.devservers.io.", "CNAME", ALB),
        _rrs("_port.dark_goldfish-2.devservers.io.", "TXT", '"0"'),
    ]


def test_missing_records_is_success_without_change_call(route53):
    assert dns_utils.delete_dns_record("never_created", "1.2.3.4", 32000)
    route53.change_resource_record_sets.assert_not_called()

