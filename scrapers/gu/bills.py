import dateutil
import lxml.html
import pytz
import re
import tempfile

from openstates.scrape import Scraper, Bill
from openstates.utils import convert_pdf


class GUBillScraper(Scraper):
    _tz = pytz.timezone("Pacific/Guam")
    # non-greedy match on bills in the "list" page
    bill_match_re = re.compile("(<p>.*?<br>)", re.DOTALL)
    res_match_re = re.compile(r"(<p align=\"left\">.*?<br>)", re.DOTALL)
    sponsors_match_re = re.compile(r"Sponsor\(s\) -(.*)<p>", re.DOTALL)
    desc_match_re = re.compile(r"^\s?<p>(.*?)<li>", re.DOTALL)
    res_desc_match_re = re.compile(r"<p align\=\"left\">([^<>]+)")
    filtered_details = ["BILL HISTORY", "Bill HISTORY", "CLERKS OFFICE", "Page 1"]
    date_re = re.compile("([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})")
    date_time_re = re.compile(
        r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}\s?\n?[0-9]{1,2}:[0-9]{2} [apAP]\.?[mM]\.?)",
    )
    committee_re = re.compile("([cC]ommittee on [a-zA-Z, \n]+)")

    def _download_pdf(self, url: str):
        res = self.get(url)
        fd = tempfile.NamedTemporaryFile()
        fd.write(res.content)
        text = convert_pdf(fd.name, type="xml")
        return text

    def _get_bill_details(self, url: str):
        data = lxml.html.fromstring(self._download_pdf(url)).xpath("//text")
        # filter out empty and obvious text we don't need
        text_only = "\n".join(
            [
                d.text
                for d in data
                if d.text
                and d.text.strip()
                and d.text.strip() not in self.filtered_details
            ]
        )
        details = {"IntroducedDate": None, "ReferredDate": None, "Committee": None}
        full_dates = self.date_time_re.findall(text_only)
        days = self.date_re.findall(text_only)
        if full_dates:
            details["IntroducedDate"] = self._tz.localize(
                dateutil.parser.parse(full_dates[1])
            )
        if len(days) > 2:
            details["ReferredDate"] = self._tz.localize(dateutil.parser.parse(days[2]))
        committee = self.committee_re.search(text_only)
        if committee:
            details["Committee"] = " ".join(
                committee.group(1).replace("\n", " ").split()
            )
        return details

    def _get_resolution_details(self, url: str):
        data = lxml.html.fromstring(self._download_pdf(url)).xpath("//text")
        # filter out empty and obvious text we don't need
        text_only = "\n".join(
            [
                d.text
                for d in data
                if d.text
                and d.text.strip()
                and d.text.strip() not in self.filtered_details
            ]
        )
        details = {
            "IntroducedDate": None,
            "PresentationDate": None,
            "AdoptedDate": None,
        }
        full_dates = self.date_time_re.findall(text_only)
        if full_dates:
            details["IntroducedDate"] = self._tz.localize(
                dateutil.parser.parse(full_dates[1])
            )
            if len(full_dates) > 1:
                details["PresentationDate"] = self._tz.localize(
                    dateutil.parser.parse(full_dates[2])
                )
            if len(full_dates) > 2:
                details["AdoptedDate"] = self._tz.localize(
                    dateutil.parser.parse(full_dates[3])
                )
        return details

    def _process_bill(self, session: str, bill: str, root_url: str):
        xml = lxml.html.fromstring(bill)
        xml.make_links_absolute(root_url)
        # Bill No. 163-37 (LS) or Bill No. 160-37 (LS) - WITHDRAWN match
        name_parts = (
            xml.xpath("//strong")[0].text.strip().removeprefix("Bill No. ").split()
        )
        name = name_parts[0].strip().removeprefix("Bill No. ")
        # bill_type = name_parts[1].strip("(").strip(")")
        bill_link = xml.xpath("//a/@href")[0]
        bill_obj = Bill(
            name,
            legislative_session=session,
            chamber="unicameral",
            title="See Introduced Link",
            classification="bill",
        )
        bill_obj.add_source(root_url, note="Bill Index")
        bill_obj.add_source(bill_link, note="Bill Introduced")
        if "WITHDRAWN" in "".join(name_parts):
            details = self._get_details(bill_link)
            if details["IntroducedDate"]:
                bill_obj.add_action("Introduced", details["IntroducedDate"])
            if details["ReferredDate"]:
                if details["Committee"]:
                    bill_obj.add_action(
                        "Referred To Committee",
                        details["ReferredDate"],
                        organization=details["Committee"],
                    )
                else:
                    bill_obj.add_action(
                        "Referred To Committee", details["ReferredDate"]
                    )

            yield bill_obj
        else:
            status = xml.xpath("//li")[0].xpath("a/@href")[0]
            bill_obj.add_document_link(url=status, note="Bill Status")
            description = (
                self.desc_match_re.search(bill).group(1).strip().split("<p>")[-1]
            )
            bill_obj.title = description.title()
            # sponsors are deliniated by / and \n, so we need to strip many characters
            sponsors = [
                s.strip("/").strip()
                for s in self.sponsors_match_re.search(bill).group(1).split("\n")
                if s.strip()
            ]
            bill_obj.add_sponsorship(
                name=sponsors[0],
                entity_type="person",
                classification="primary",
                primary=True,
            )
            for sponsor in sponsors[1:]:
                bill_obj.add_sponsorship(
                    name=sponsor,
                    entity_type="person",
                    classification="cosponsor",
                    primary=False,
                )

            for link in xml.xpath("//li")[1:]:
                url = link.xpath("a/@href")[0]
                title = link.xpath("a")[0].text
                bill_obj.add_document_link(url=url, note=title)

            # status PDF has introduced/passed/etc. dates
            details = self._get_details(status)
            if details["IntroducedDate"]:
                bill_obj.add_action("Introduced", details["IntroducedDate"])
            if details["ReferredDate"]:
                if details["Committee"]:
                    bill_obj.add_action(
                        "Referred To Committee",
                        details["ReferredDate"],
                        organization=details["Committee"],
                    )
                else:
                    bill_obj.add_action(
                        "Referred To Committee", details["ReferredDate"]
                    )
            yield bill_obj

    def _process_resolution(self, session: str, bill: str, root_url: str):
        xml = lxml.html.fromstring(bill)
        xml.make_links_absolute(root_url)
        # Bill No. 163-37 (LS) or Bill No. 160-37 (LS) - WITHDRAWN match
        res_parts = xml.xpath("//a")[0].text.removeprefix("Res. No. ").split()
        name = res_parts[0].strip()
        # res_type = res_parts[1].strip(")").strip("(")
        bill_link = xml.xpath("//a/@href")[0]
        bill_obj = Bill(
            name,
            legislative_session=session,
            chamber="unicameral",
            title="See Introduced Link",
            classification="resolution",
        )
        bill_obj.add_source(root_url, note="Bill Index")
        bill_obj.add_source(bill_link, note="Bill Introduced")
        description = self.res_desc_match_re.search(bill).group(1)
        bill_obj.title = description.title()
        # sponsors are deliniated by / and \n, so we need to strip many characters
        sponsors = [
            s.strip("/").strip()
            for s in self.sponsors_match_re.search(bill).group(1).split("\n")
            if s.strip()
        ]
        result = None
        result_date = None
        if "-" in sponsors[-1]:
            name, result_data = sponsors[-1].split("-")
            sponsors[-1] = name
            result, result_date = result_data.split()
        if result and result_date:
            bill_obj.add_action(result, result_date)

        bill_obj.add_sponsorship(
            name=sponsors[0],
            entity_type="person",
            classification="primary",
            primary=True,
        )
        for sponsor in sponsors[1:]:
            bill_obj.add_sponsorship(
                name=sponsor,
                entity_type="person",
                classification="cosponsor",
                primary=False,
            )

        # status PDF has introduced/passed/etc. dates
        details = self._get_details(bill_link)
        if details["IntroducedDate"]:
            bill_obj.add_action("Introduced", details["IntroducedDate"])
        if details["PresentationDate"]:
            bill_obj.add_action("Presented", details["ReferredDate"])
        if details["AdoptedDate"]:
            bill_obj.add_action("Adopted", details["ReferredDate"])
        yield bill_obj

    def scrape(self, session):
        """
        bills_url = f"https://guamlegislature.com/{session}_Guam_Legislature/{session}_bills_intro_content.htm"
        doc = self.get(bills_url).text.split("-->")[-1]
        for bill in self.bill_match_re.findall(doc):
            yield self._process_bill(session, bill, bills_url)
        """

        # resolutions are at a separate address
        res_url = f"https://guamlegislature.com/{session}_Guam_Legislature/{session}_res_content.htm"
        doc = self.get(res_url).text.split("-->")[-2]
        for resolution in self.res_match_re.findall(doc):
            yield self._process_resolution(session, resolution, res_url)
