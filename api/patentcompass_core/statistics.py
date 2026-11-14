"""Reproducible application-level statistics over detached patent records."""

from collections import defaultdict
from collections.abc import Mapping

from .contracts import CapabilityError, PatentRecord


COUNTING_METHOD = {"unit": "application", "applicants": "full_count", "ipc": "multi_label_full_count",
                   "year": "application_date", "coverage": "imported_sample_only"}


def _value(row, key, default=None):
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def compute_statistics(records, publications=None, versions=None) -> dict:
    """Count each application once and each distinct applicant/IPC/country once per application.

    Publication rows can be supplied separately, or embedded in each input record.
    The result retains the existing analysis checkpoint structure and contains JSON values only.
    """
    patents = [PatentRecord.model_validate(row) for row in records]
    if any(patent.application_date is None for patent in patents):
        raise CapabilityError(422, "STATISTICS_DATE_REQUIRED", "统计分析需要每条申请的申请日")
    if publications is None:
        publications = [{**row, "application_id": patent.id}
                        for patent in patents for row in patent.publications]
    publications = list(publications)
    publication_map = defaultdict(list)
    for publication in publications:
        publication_map[_value(publication, "application_id")].append(_value(publication, "publication_number"))
    years, applicants, ipc, countries = (defaultdict(list) for _ in range(4))
    for patent in patents:
        years[str(patent.application_date.year)].append(patent.id)
        for source, target in ((patent.applicants, applicants), (patent.ipc_codes, ipc), (patent.countries, countries)):
            for value in dict.fromkeys(source):
                target[value].append(patent.id)

    def ranking(mapping, key):
        return [{key: value, "count": len(ids)} for value, ids in
                sorted(mapping.items(), key=lambda pair: (-len(pair[1]), pair[0]))]

    versions = list(versions or [])
    summary = {"application_count": len(patents), "publication_count": len(publications),
               "abstract_count": sum(bool(p.abstract) for p in patents),
               "yearly": [{"year": int(year), "count": len(ids)} for year, ids in sorted(years.items())],
               "applicants": ranking(applicants, "name"), "ipc": ranking(ipc, "code"),
               "country": ranking(countries, "country"), "quality": _value(versions[0], "quality", {}) if versions else {},
               "source_metadata": _value(versions[0], "source_metadata", {}) if versions else {},
               "counting_method": dict(COUNTING_METHOD)}
    representative = [{"application_id": p.id, "application_number": p.application_number,
                       "publication_numbers": sorted(publication_map[p.id]), "title": p.title, "abstract": p.abstract,
                       "source_sheet": p.source_sheet, "source_row": p.source_row, "dataset_version_id": p.dataset_version_id}
                      for p in patents[:10]]
    return {"summary": summary, "patent_ids": [patent.id for patent in patents],
            "selections": {"yearly": dict(years), "applicants": dict(applicants), "ipc": dict(ipc), "country": dict(countries)},
            "representative": representative}
