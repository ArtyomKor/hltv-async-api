from typing import Any, List
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup
import asyncio
from asyncio import get_running_loop
from functools import partial

from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientProxyConnectionError, ClientResponseError, ClientOSError, \
    ServerDisconnectedError, ClientHttpProxyError

import logging


class Hltv:
    def __init__(self, max_delay: int = 15,
                 timeout: int = 5,
                 use_proxy: bool = False,
                 proxy_path: str | None = None,
                 proxy_list: list | None = None,
                 debug: bool = False):
        self.headers = {
            "referer": "https://www.hltv.org/stats",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "hltvTimeZone": "UTC"
        }

        self.MAX_DELAY = max_delay
        self.timeout = timeout
        self.USE_PROXY = use_proxy
        self.PROXY_PATH = proxy_path
        self.PROXY_LIST = proxy_list
        self.DEBUG = debug
        self._configure_logging()
        self.logger = logging.getLogger(__name__)

    def _configure_logging(self):
        level = logging.DEBUG if self.DEBUG else logging.INFO
        logging.basicConfig(level=level)

    def config(self, max_delay: int | None = None,
               timeout: int | None = None,
               use_proxy: bool | None = None,
               proxy_file_path: str | None = None,
               proxy_list: list | None = None,
               debug: bool | None = None):
        if max_delay:
            self.MAX_DELAY = max_delay
        if timeout:
            self.timeout = timeout
        if use_proxy:
            self.USE_PROXY = use_proxy
        if proxy_file_path:
            self.PROXY_FILE_PATH = proxy_file_path
        if proxy_list:
            self.PROXY_LIST = proxy_list

        if debug:
            self.DEBUG = debug
            self._configure_logging()

    def get_proxy(self):
        if self.PROXY_PATH:
            with open(self.PROXY_PATH, "r") as file:
                proxy = file.readline().strip()
                if proxy:
                    return proxy
        else:
            return self.PROXY_LIST[0]

    def switch_proxy(self, proxy):
        if self.PROXY_PATH:
            with open(self.PROXY_PATH, "r+") as file:
                proxies = file.readlines()
                file.seek(0)
                for line in proxies:
                    if line.strip() != proxy:
                        file.write(line)
                file.write(proxy + "\n")
        else:
            self.PROXY_LIST = self.PROXY_LIST[1:] + [self.PROXY_LIST[0]]

    def f(self, result):
        return BeautifulSoup(result, "lxml")

    async def cloudflare_check(self, result) -> bool:
        page = self.f(result)
        challenge_page = page.find(id="challenge-error-title")
        if challenge_page is not None:
            if "Enable JavaScript and cookies to continue" == challenge_page.get_text():
                return True
        return False

    async def call_again(self, url, proxy, delay):
        if self.USE_PROXY:
            self.logger.info(f"Switching proxy {proxy}")
            self.switch_proxy(proxy)
            self.logger.debug(f"New proxy: {self.get_proxy()}")
            return await self.fetch(url)
        else:
            if delay < self.MAX_DELAY:
                delay += 1
            else:
                self.logger.warning("Reached max delay limit, try to use Proxy")
            self.logger.info(f"Calling again, increasing delay to {delay}s")
            return await self.fetch(url, delay=delay)

    async def fetch(self, url, delay: int = 0):
        proxy = ''
        # setup new proxy
        if self.USE_PROXY:
            proxy = self.get_proxy()

        # delay, only for non proxy users. (default = 1-15s)
        await asyncio.sleep(delay)
        async with ClientSession() as session:
            try:
                async with session.get(url, headers=self.headers, proxy=proxy, timeout=self.timeout) as response:
                    self.logger.info(f"Fetching {url}, code: {response.status}")
                    if response.status == 403 or response.status == 404:
                        self.logger.debug("Got 403 forbitten")
                        return await self.call_again(url, proxy, delay)

                    # checking for challenge page.
                    result = await response.text()
                    if await self.cloudflare_check(result):
                        self.logger.debug("Got cloudflare challange page")
                        return await self.call_again(url, proxy, delay)

                    # running executed loop if everything okay
                    loop = get_running_loop()
                    parsed = await loop.run_in_executor(None, partial(self.f, result))
                    return parsed
            except (ClientProxyConnectionError, ClientResponseError, ClientOSError,
                    ServerDisconnectedError, TimeoutError, ClientHttpProxyError) as e:
                self.logger.debug(f"Got 404 ({e}) ({proxy})")
                return await self.call_again(url, proxy, delay)

    def save_error(self, page):
        with open("error.html", "w") as file:
            file.write(page.prettify())


    def normalize_date(self, parts) -> str:
        month_abbreviations = {
            'Jan': '1', 'Feb': '2', 'Mar': '3', 'Apr': '4',
            'May': '5', 'Jun': '6', 'Jul': '7', 'Aug': '8',
            'Sep': '9', 'Oct': '10', 'Nov': '11', 'Dec': '12'
        }
        month = month_abbreviations[parts[0]]
        day = parts[1][:-2]
        return day + '-' + month

    async def get_live_matches(self):
        """returns a list of all LIVE matches on HLTV along with the maps being played and the star ratings"""
        r = await self.fetch("https://www.hltv.org/matches")

        live_matches = r.find("div", {'class', "liveMatchesContainer"})
        if live_matches is None:
            return []
        else:
            teams = [line.getText() for line in live_matches.find_all("div", {'class', "matchTeamName text-ellipsis"})]
            matches = [(team1, team2) for team1, team2 in tuple(zip(teams, teams[1:]))[::2]]
            liveMatchContainer = live_matches.find_all("div", {'class', "liveMatch-container"})
            maps = [str(line.get('data-maps')).split(',') for line in liveMatchContainer]
            stars = [line.get('stars') for line in liveMatchContainer]
            return [{'teams': teams, 'maps': maps, 'stars': stars} for teams, maps, stars in zip(matches, maps, stars)]

    async def get_upcoming_matches(self):
        """returns a list of all upcoming matches on HLTV"""
        r = await self.fetch("https://www.hltv.org/matches")
        try:
            teams = [line.getText() for line in r.find("div",
                                                       {'class', "upcomingMatchesContainer"}).find_all(
                class_=lambda v: v is not None and (v == "team text-ellipsis" or v == "matchTeamName text-ellipsis"))]
        except AttributeError:
            return None
        return [(team1, team2) for team1, team2 in tuple(zip(teams, teams[1:]))[::2]]

    async def get_important_upcoming_matches(self, star_rating=1):
        """returns a list of all upcoming matches on HLTV with the star rating argument (should be between 0 and 5)"""
        r = await self.fetch("https://www.hltv.org/matches")

        teams = [line.getText() for line in r.find("div",
                                                   {'class', "upcomingMatchesContainer"}).find_all(
            class_=lambda v: v is not None and (v == "team text-ellipsis" or v == "matchTeamName text-ellipsis"))]
        stars = [int(line.get('stars')) for line in r.find("div",
                                                           {'class', "upcomingMatchesContainer"}).find_all("div", {"class",
                                                                                                                   "upcomingMatch "})
                 if line.get('team1') is not None]
        matches = [(team1, team2) for team1, team2 in tuple(zip(teams, teams[1:]))[::2]]
        # assert len(matches) == len(stars), "Internal Exception :: get_important_upcoming_matches() :: misMatches detected"
        return [match for match, star in zip(matches, stars) if star == star_rating]

    async def get_big_results(self, offset=0) -> list[dict[str, Any]] | None:
        """returns a list of results from past 100 matches on HLTV starting from the offset param"""
        r = await self.fetch("https://www.hltv.org/results?offset=" + str(offset))

        big_results = []
        big_res = r.find("div", {'class', "big-results"}).find_all("div", {"class", "result-con"})
        if not big_res:
            return None
        for res in big_res:
            team1 = res.find("div", class_="team").text.strip()
            team2 = res.find("div", class_="team team-won").text.strip()

            scores = res.find("td", class_="result-score").text.strip().split('-')
            s_t1 = scores[0].strip()
            s_t2 = scores[1].strip()

            big_results.append({
                'team1': team1,
                'team2': team2,
                'score1': s_t1,
                'score2': s_t2,
            })

        return big_results

    async def get_event_results(self, event: int | str) -> list[dict[str, Any]]:
        r = await self.fetch("https://www.hltv.org/results?event=" + str(event))

        match_results = []

        for result in r.find("div", {'class', 'results-holder'}).find_all("div", {'class', 'results-sublist'}):
            date = result.find("span", class_="standard-headline").text.strip()
            matches = result.find_all("div", class_="result-con")

            for match in matches:
                teams = match.find_all("div", class_="team")
                team1 = teams[0].text.strip()
                team2 = teams[1].text.strip()

                scores = match.find("td", class_="result-score").text.strip().split('-')
                score_t1 = scores[0].strip()
                score_t2 = scores[1].strip()

                match_results.append({
                    'date': date,
                    'team1': team1,
                    'team2': team2,
                    'score1': score_t1,
                    'score2': score_t2,
                })

        return match_results

    async def get_event_matches(self, event_id: str | int):
        r = await self.fetch("https://www.hltv.org/events/" + str(event_id) + "/matches")

        live_matches: List | Any
        matches = []
        try:
            live_matches = r.find("div", {'class', 'liveMatchesSection'}).find_all("div", {'class', 'liveMatch'})
        except AttributeError:
            live_matches = []

        for live in live_matches:
            teams = live.find_all("div", class_="matchTeamName text-ellipsis")
            team1 = teams[0].text.strip()
            team2 = teams[1].text.strip()

                # TODO FIX SCORES
            try:
                scores = live.find("td", class_="matchTeamScore").text.strip().split('-')
                score_team1 = scores[0].strip()
                score_team2 = scores[1].strip()
            except AttributeError:
                score_team1 = 0
                score_team2 = 0

            matches.append({
                    'team1': team1,
                    'team2': team2,
                    'date': 'LIVE'
            })

        for date_sect in r.find_all('div', {'class': 'upcomingMatchesSection'}):
            date_ = date_sect.find('span', {'class': 'matchDayHeadline'}).text.split(' ')[-1]
            for match in date_sect.find_all('div', {'class': 'upcomingMatch'}):
                teams = match.find_all("div", class_="matchTeamName text-ellipsis")
                if teams or len(teams) > 1:

                    team1 = teams[0].text.strip()
                    team2 = teams[1].text.strip()

                    time_ = match.find('div', {'class', 'matchTime'}).text

                    matches.append({
                        'team1': team1,
                        'team2': team2,
                        'date': date_ + " " + time_
                    })
                else:
                    break

        return matches

    async def get_events(self, outgoing=True, future=True, max_events=10):
        """Returns events
        :params:
        outgoing - include live tournaments
        future - include future tournamets
        max_events - use only if future=True
        :return:
        [('id', 'title', 'startdate', 'enddate')]
        """

        r = await self.fetch('https://www.hltv.org/events')

        events = []
        if outgoing:
            for event in r.find('div', {'class': 'tab-content', 'id': 'TODAY'}).find_all('a', {
                'class': 'a-reset ongoing-event'}):
                event_name = event.find('div', {'class': 'text-ellipsis'}).text.strip()
                event_start_date = self.normalize_date(event.find('span', {'data-time-format': 'MMM do'}).text.strip().split())

                event_end_date = self.normalize_date(event.find_all('span', {'data-time-format': 'MMM do'})[1].text.strip().split())
                event_id = event['href'].split('/')[-2]

                events.append({
                    'id': event_id,
                    'name': event_name,
                    'start_date': event_start_date,
                    'end_date': event_end_date,
                })

        if future:
            for i, big_event_div in enumerate(r.find_all('div', {'class': 'big-events'}, start=1)):
                for event in big_event_div.find_all('a', {'class': 'a-reset standard-box big-event'}):

                    if i >= max_events:
                        break

                    event_id = event['href'].split('/')[-2]
                    event_name = event.find('div', {'class': 'big-event-name'}).text.strip()
                    # event_location = event.find('span', {'class': 'big-event-location'}).text.strip()
                    event_start_date = self.normalize_date(event.find('span', {'class': ''}).text.strip().split())
                    event_end_date = self.normalize_date(event.find('span', {'class': ''}).text.strip().split())

                    events.append({
                        'id': event_id,
                        'title': event_name,
                        'start_date': event_start_date,
                        'end_date': event_end_date
                    })

        return events

    async def get_event_info(self, event_id: str | int, event_title: str):
        r = await self.fetch(f"https://www.hltv.org/events/{str(event_id)}/{event_title.replace(' ', '-')}")

        event_date_div = r.find('td', {'class', 'eventdate'}).find_all('span')

        event_start = self.normalize_date(event_date_div[0].text.split())
        event_end = self.normalize_date(event_date_div[1].text.split()[1:-1])

        prize = r.find('td', {'class', 'prizepool text-ellipsis'}).text

        team_num = r.find('td', {'class', 'teamsNumber'}).text
        
        location = r.find('td', {'class', 'location gtSmartphone-only'}).get_text().replace('\n', '')

        try:
            group_div = r.find('div', {'class', 'groups-container'})
            groups = []
            for group in group_div.find_all('table', {'class': 'table standard-box'}):
                group_name = group.find('td', {'class': 'table-header group-name'}).text
                teams = []
                for team in group.find_all('div', 'text-ellipsis'):
                    teams.append(team.find('a').text)
                groups.append({group_name: teams})
        except AttributeError:
            groups = []

        return (event_id, event_title, event_start, event_end, prize, team_num, location, groups)

    async def get_top_teams(self, max_teams=30):
        """
        returns a list of the top 1-30 teams
        :params:
        max_teams: int = 30
        :return:
        [('rank','title','points', 'change', 'id')]
        change - difference between last ranking update
        """
        today = date.today()
        current_weekday = today.weekday()
        last_monday = today - timedelta(days=current_weekday)
        
        teams = []

        r = await self.fetch("https://www.hltv.org/ranking/teams/" + last_monday.strftime('%Y/%B/%d').lower())

        try:
            for i, team in enumerate(r.find_all("div", {'class': "ranked-team standard-box"}), start=1):
                if i > max_teams:
                    break

                rank = team.find('span', {'class': 'position'}).text[1:]
                title_div: Any
                if rank != '1':
                    title_div = team.find('div', {'class': 'teamLine sectionTeamPlayers'})
                else:
                    title_div = team.find('div', {'class': 'teamLine sectionTeamPlayers teamLineExpanded'})

                title = title_div.find('span', {'class': 'name'}).text
                points = title_div.find('span', {'class': 'points'}).text.split(' ', 1)[0][1:]

                id = team.find('a', {'class': 'details moreLink'})['href'].split('/')[-1]

                changes = {'change positive', 'change neutral', 'change negative'}
                change = ''
                for change_ in changes:
                    try:
                        change = team.find('div', {'class', change_}).text
                        break
                    except AttributeError:
                        pass

                teams.append({
                    'rank': rank,
                    'title': title,
                    'points': points,
                    'change': change,
                    'id': id
                })
        except AttributeError:
            raise AttributeError("Parsing error, probably page not fully loaded")

        return teams

    async def get_team_info(self, team_id: int | str, title: str) -> tuple | None:
        """
        Returns Information about team
        :params:
        team_id: int | str
        title: str
        :returns:
        (team_id, title, rank, players, coach, age, weeks, last_trophy, total_trophys) | None
        weeks - weeks in top 20
        """
        r = await self.fetch("https://www.hltv.org/team/" + str(team_id) + '/' + title.replace(' ', '-'))
        players = []
        try:
            for player in r.find_all('span', {'class': 'text-ellipsis bold'}):
                players.append(player.text)

            rank = '0'
            weeks = '0'
            age = '0'
            coach = ''

            for i, stat in enumerate(r.find_all('div', {'class': 'profile-team-stat'}), start=1):
                if i == 1:
                    rank = stat.find('a').text[1:]
                elif i == 2:
                    weeks = stat.find('span', {'class': 'right'}).text
                elif i == 3:
                    age = stat.find('span', {'class': 'right'}).text
                elif i == 4:
                    coach = stat.find('span', {'class': 'bold a-default'}).text[1:-1]

            last_trophy = None
            total_trophys = None
            try:
                last_trophy = r.find('div', {'class': 'trophyHolder'}).find('span')['title']
                total_trophys = len(r.find_all('div', {'class': 'trophyHolder'}))
            except AttributeError:
                pass

            return (team_id, title, rank, players, coach, age, weeks, last_trophy, total_trophys)
        except AttributeError:
            raise AttributeError("Parsing error, probably page not fully loaded")

    async def get_best_players(self, top=40):
        """
        returns a list of the top (1-40) players in top 20 at the year
        :params:
        top: int = 40
        :returns:
        ('rank', 'name', 'team', 'maps', 'rating')
        maps - maps played
        """
        year = datetime.strftime(datetime.utcnow(), '%Y')
        r = await self.fetch(
            f"https://www.hltv.org/stats/players?startDate={year}-01-01&endDate={year}-12-31&rankingFilter=Top20")

        players = []
        rank = 1
        try:
            for player in r.find('tbody').find_all('tr'):
                name = player.find('td', {'class', 'playerCol'}).find('a').text
                team = player.find('td', {'class', 'teamCol'})['data-sort']

                maps = player.find('td', {'class', 'statsDetail'}).text

                ratings = {'ratingCol ratingPositive', 'ratingCol ratingNeutral', 'ratingCol ratingNegative'}
                rating = 'ERROR'
                for rat in ratings:
                    try:
                        rating = player.find('td', {'class', rat}).text

                        break
                    except AttributeError:
                        pass

                players.append({
                    'rank': rank,
                    'name': name,
                    'team': team,
                    'maps': maps,
                    'rating': rating,
                })
                rank += 1
                if rank > top:
                    break
        except AttributeError:
            raise AttributeError("Parsing error, probably page not fully loaded")

        return players

    # TODO WRITE
    async def get_last_news(self):
        return []


async def test():
    hltv = Hltv()
    print(await hltv.get_event_info(7148, 'pgl-cs2-major-copenhagen-2024'))

if __name__ == "__main__":
    asyncio.run(test())

