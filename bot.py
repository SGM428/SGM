"""
리그오브레전드 내전 팀 밸런싱 디스코드 봇
- /내전모집 라인선택 : 신청하기/취소하기 버튼(모달)으로 라인/티어를 입력받아 신청받는 모집 시작
- /내전모집 채우기 : 신청하기/취소하기 버튼(모달)으로 주라인/부라인/티어를 입력받아 신청받는 모집 시작
- /내전팀 : 10명이 다 모이면 팀을 나눔 (두 모집 방식 모두 지원)
- /모의내전테스트 : [관리자] 테스트용 가짜 10명으로 모집 없이 바로 팀 결과를 만듦
- /내전모집취소 : 진행 중인 모집을 취소
- /내전로그설정 : 신청/취소 로그를 보낼 채널 지정
- /서버퇴장로그설정 : [관리자] 유저가 서버를 나가면 알려줄 채널 지정
- /먹물코인채널설정 : [관리자] 먹물코인 잔액 데이터를 저장할 채널 지정
- !먹물코인 : 내 먹물코인 잔액 확인 (등록 안 된 사람은 기본 10,000)
- !송금 @유저 금액 : 먹물코인 송금 (최소 100만부터, 보내는 사람 잔액이 10,000 밑으로는 안 내려감)
- !블랙잭 베팅액 : 히트/스테이 버튼으로 진행하는 블랙잭 (실행한 본인만 버튼 조작 가능, 베팅액만큼 먹물코인 정산)
- !가위바위보 베팅액 : AI전(승2배/무1배/패0배) 또는 내기대전(PvP, 도전자와 붙어서 이긴 쪽이 베팅액 전부 획득)
- !하이로우 베팅액 : 다음 카드가 높다/낮다/같다(10:1) 맞히는 1클릭 게임
- /내전결과채널설정 : 팀 결과 + 결과입력 버튼을 올릴 채널 지정
- /내전전적채널설정 : 누적 승/패 전적을 기록할 채널 지정
- /전적검색 : (누구나) 특정 롤 닉네임의 누적 전적 조회
- /전적수정 : [관리자] 특정 롤 닉네임의 전적을 직접 수정
- /전적삭제 : [관리자] 특정 롤 닉네임의 전적 기록을 삭제
- /내전라인고정 : [비공개] '채우기' 모집 중인 채널에서 이미 신청한 닉네임을 특정 라인으로 고정 (해당 모집에만 적용, 재시작 시 초기화)
- /내전라인교체, /내전팀묶기, /내전팀분리, /내전대타, /내전라인요청, /내전다시섞기, /내전위치교환 : /내전팀 이후 팀 구성을 메모리(active_matches) 기반으로 조정
- /내전확정 : 팀 결과를 확정 (결과채널이 있으면 그쪽으로 넘어가고 더 이상 조정 불가)
- /내전변수확인 : [관리자 전용, 나만 보기] 이 채널의 매치 메모리 상태 확인 + 선호 라인 수정 + 조건 개별 삭제
"""

import os
import re
import random
import asyncio
import itertools
from dataclasses import dataclass, asdict
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from scipy.optimize import linear_sum_assignment

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")  # 설정하면 그 서버에는 명령어가 즉시 반영됨

# ---------------------------------------------------------------------------
# 티어 점수 체계
# ---------------------------------------------------------------------------

TIERS = ["아이언", "브론즈", "실버", "골드", "플래티넘", "에메랄드", "다이아", "마스터", "그랜드마스터", "챌린저"]
DIVISIONS = ["4", "3", "2", "1"]
LINES = ["탑", "정글", "미드", "원딜", "서폿"]

# 아이언4(1점) ~ 다이아1(28점)까지는 세부단계마다 1점씩 증가.
# 마스터 이상은 세부단계가 없으므로 별도 고정 점수.
_NO_DIVISION_SCORE = {
    "마스터": 29,
    "그랜드마스터": 32,
    "챌린저": 35,
}

# 줄임말로 입력해도 인식되게 하는 별칭 (전체 티어 대상)
_TIER_ALIASES = {
    "아": "아이언",
    "브": "브론즈",
    "실": "실버",
    "골": "골드",
    "플": "플래티넘",
    "플레": "플래티넘",
    "플래": "플래티넘",
    "플레티넘": "플래티넘",
    "에": "에메랄드",
    "에메": "에메랄드",
    "다": "다이아",
    "마": "마스터",
    "그마": "그랜드마스터",
    "챌": "챌린저",
}


def tier_score(tier: str, division: Optional[str]) -> int:
    if tier in _NO_DIVISION_SCORE:
        return _NO_DIVISION_SCORE[tier]
    tier_index = TIERS.index(tier)  # 0=아이언 ... 6=다이아
    division_index = DIVISIONS.index(division)  # "4"->0, "3"->1, "2"->2, "1"->3
    return tier_index * 4 + division_index + 1


def tier_label(tier: str, division: Optional[str], lp: Optional[int] = None) -> str:
    base = tier if tier in _NO_DIVISION_SCORE else f"{tier}{division}"
    if lp is not None:
        return f"{base} {lp}"
    return base


def parse_tier_text(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """'골드2', '골드 2', '마스터', '그마', '챌', '아4' 같은 입력을 (티어, 세부단계, 에러메시지)로 파싱한다."""
    text = text.strip()
    if not text:
        return None, None, "티어를 입력해주세요. (예: 골드2, 마스터, 그마, 챌)"

    def resolve(name: str) -> str:
        return _TIER_ALIASES.get(name, name)

    # 전체가 별칭/티어명 하나인 경우 (주로 마스터 이상, 세부단계 없는 티어)
    resolved_whole = resolve(text)
    if resolved_whole in _NO_DIVISION_SCORE:
        return resolved_whole, None, None

    # 뒤에 붙은 세부단계 숫자와 티어 이름 분리 ("골드2", "골드 2", "아4" 등 지원)
    if text[-1] in DIVISIONS:
        tier_name_raw = text[:-1].strip()
        division_value = text[-1]
        tier_name = resolve(tier_name_raw)
        if tier_name in TIERS and tier_name not in _NO_DIVISION_SCORE:
            return tier_name, division_value, None

    if resolved_whole in TIERS:
        return None, None, f"'{text}'는 세부단계도 같이 적어주세요. (예: {resolved_whole}2)"

    return None, None, f"알 수 없는 티어예요: '{text}' (예: 골드2, 다이아4, 마스터, 그마, 챌)"


@dataclass
class PlayerInfo:
    name: str
    tier: str
    division: Optional[str]
    line1: str
    line2: str
    lp: Optional[int] = None

    @property
    def score(self) -> int:
        # LP는 표기/랭킹에만 쓰이고 팀 밸런싱 점수에는 영향을 주지 않음
        return tier_score(self.tier, self.division)

    @property
    def tier_text(self) -> str:
        return tier_label(self.tier, self.division, self.lp)


# ---------------------------------------------------------------------------
# 팀 밸런싱 알고리즘
# ---------------------------------------------------------------------------

def assign_lines_with_pins(
    players: list[PlayerInfo],
    pinned: dict[str, str],
    hard_pinned: Optional[dict[str, str]] = None,
    avoid: Optional[dict[str, set]] = None,
) -> dict[int, str]:
    """
    관리자가 고정해둔 (닉네임 -> 라인)을 먼저 확정하고, 나머지 인원만 헝가리안 알고리즘으로 남은 자리에 배정한다.
    pinned(소프트 고정)은 해당 인원이 그 라인을 주라인/부라인 중 하나로 신청했을 때만 유효하다.
    hard_pinned(강제 고정)은 신청 라인과 무관하게 무조건 그 라인으로 확정한다
    (/내전라인교체, /내전라인요청처럼 관리자가 이미 명시적으로 강제 이동시킨 결과를 재현할 때 사용).
    avoid는 (닉네임 -> 피하고 싶은 라인 집합). 대안이 있으면 그쪽으로 유도하는 정도로, 절대 금지는 아니다
    (예: /내전팀묶기로 같은 라인에 겹친 사람을 다른 라인으로 밀어내볼 때 사용).
    """
    hard_pinned = hard_pinned or {}
    avoid = avoid or {}
    forced: dict[int, str] = {}
    remaining_capacity = {line: 2 for line in LINES}
    free_indices = []

    for idx, p in enumerate(players):
        line = hard_pinned.get(p.name.strip().lower())
        if line and remaining_capacity[line] > 0:
            forced[idx] = line
            remaining_capacity[line] -= 1

    for idx, p in enumerate(players):
        if idx in forced:
            continue
        line = pinned.get(p.name.strip().lower())
        if line and line in (p.line1, p.line2) and remaining_capacity[line] > 0:
            forced[idx] = line
            remaining_capacity[line] -= 1
        else:
            free_indices.append(idx)

    slots = []
    for line in LINES:
        slots.extend([line] * remaining_capacity[line])

    if free_indices:
        cost = [[0] * len(slots) for _ in free_indices]
        for i, idx in enumerate(free_indices):
            p = players[idx]
            avoid_lines = avoid.get(p.name.strip().lower(), set())
            for j, line in enumerate(slots):
                if line in avoid_lines:
                    cost[i][j] = 50  # 완전히 막지는 않되, 대안이 있으면 그쪽을 훨씬 선호하게 만듦
                elif line == p.line1:
                    cost[i][j] = 0
                elif line == p.line2:
                    cost[i][j] = 1
                else:
                    cost[i][j] = 5

        row_ind, col_ind = linear_sum_assignment(cost)
        for r, c in zip(row_ind, col_ind):
            forced[free_indices[r]] = slots[c]

    return forced


def split_teams(players: list[PlayerInfo], line_assignment: dict[int, str]):
    """
    라인 배정이 끝난 10명을, 각 라인마다 한 명씩 팀A/팀B로 나누되
    팀 점수 합의 차이가 최소가 되는 조합을 찾는다.
    """
    by_line: dict[str, list[int]] = {line: [] for line in LINES}
    for idx, line in line_assignment.items():
        by_line[line].append(idx)

    best_diff = None
    best_combo = None

    # 각 라인마다 2명 중 누가 팀A로 갈지 -> 2^5 = 32가지 경우의 수
    for combo in itertools.product([0, 1], repeat=len(LINES)):
        team_a = []
        team_b = []
        for line, choice in zip(LINES, combo):
            pair = by_line[line]
            a_idx, b_idx = (pair[0], pair[1]) if choice == 0 else (pair[1], pair[0])
            team_a.append((line, a_idx))
            team_b.append((line, b_idx))

        score_a = sum(players[i].score for _, i in team_a)
        score_b = sum(players[i].score for _, i in team_b)
        diff = abs(score_a - score_b)

        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_combo = (team_a, team_b, score_a, score_b)

    return best_combo  # (team_a, team_b, score_a, score_b)


def format_result(players: list[PlayerInfo], team_a, team_b, score_a, score_b) -> str:
    line_order = {line: i for i, line in enumerate(LINES)}
    team_a_sorted = sorted(team_a, key=lambda x: line_order[x[0]])
    team_b_sorted = sorted(team_b, key=lambda x: line_order[x[0]])

    lines_out = ["**🔵 팀 A**"]
    for line, idx in team_a_sorted:
        p = players[idx]
        lines_out.append(f"- {line}: {p.name} ({p.tier_text})")
    lines_out.append(f"총점: {score_a}")
    lines_out.append("")
    lines_out.append("**🔴 팀 B**")
    for line, idx in team_b_sorted:
        p = players[idx]
        lines_out.append(f"- {line}: {p.name} ({p.tier_text})")
    lines_out.append(f"총점: {score_b}")
    lines_out.append("")
    lines_out.append(f"점수 차이: {abs(score_a - score_b)}")
    return "\n".join(lines_out)


# ---------------------------------------------------------------------------
# /내전팀 이후 팀 구성을 조정하는 기능
# (active_matches 메모리에 있는 상태를 대상으로 함. 대부분의 명령어는 조건만 메모리에 기록하고,
#  /내전다시섞기를 실행할 때 실제로 계산해서 새 메시지로 결과를 올림. /내전위치교환만 예외로 즉시 반영됨)
# ---------------------------------------------------------------------------

async def _fetch_active_match_message(interaction: discord.Interaction, match: dict):
    """active_matches에 적힌 message_id로 실제 메시지를 가져온다. 없으면 None."""
    try:
        return await interaction.channel.fetch_message(match["message_id"])
    except (discord.NotFound, discord.Forbidden):
        return None


def _find_player_index(players: list[PlayerInfo], nickname: str) -> Optional[int]:
    key = nickname.strip().lower()
    for i, p in enumerate(players):
        if p.name.strip().lower() == key:
            return i
    return None


def _describe_constraints(stored: dict) -> str:
    parts = []
    for group in stored.get("same_groups", []):
        parts.append(f"{'·'.join(group)}: 같은 팀")
    for a, b in stored.get("diff_pairs", []):
        parts.append(f"{a}·{b}: 다른 팀")
    for name, line in stored.get("fixed_lines", {}).items():
        parts.append(f"{name}: {line} 고정")
    return ", ".join(parts) if parts else "(걸려있는 조건 없음)"


def _validate_fixed_lines_capacity(fixed_lines: dict) -> Optional[str]:
    """한 라인에는 최대 2명까지만 고정될 수 있다. 초과하면 어느 라인이 문제인지 담은 안내 문구를 반환한다."""
    counts: dict[str, int] = {}
    for line in fixed_lines.values():
        counts[line] = counts.get(line, 0) + 1
    overflowing = [line for line, c in counts.items() if c > 2]
    if not overflowing:
        return None
    return (
        "라인 고정 조건에 문제가 있어요 — 다음 라인에 3명 이상이 고정되어 있어서 배치할 수 없어요: "
        + ", ".join(overflowing)
        + ". /내전변수확인에서 조건을 정리해주세요."
    )


def _resolve_same_group_line_clashes(
    players: list[PlayerInfo],
    line_assignment: dict[int, str],
    hard_pinned: dict[str, str],
    same_groups: list[list[str]],
) -> dict[int, str]:
    """/내전팀묶기로 묶인 사람들이 같은 라인에 겹쳐서 구조적으로 같은 팀이 될 수 없는 상태면,
    그중 대안 라인(부라인 등)이 있는 쪽을 다른 라인으로 유도해서 다시 배정해본다."""
    avoid: dict[str, set] = {}
    current = line_assignment

    for _ in range(6):
        conflict_line = None
        conflict_name = None

        for group in same_groups:
            by_line: dict[str, list[str]] = {}
            for name in group:
                idx = _find_player_index(players, name)
                if idx is None:
                    continue
                by_line.setdefault(current.get(idx), []).append(name)
            clashing = [names for names in by_line.values() if len(names) >= 2]
            if clashing:
                conflict_line = next(l for l, names in by_line.items() if len(names) >= 2)
                clashing_names = by_line[conflict_line]
                # 대안 라인(주라인 != 부라인)이 있는 사람을 우선 회피 대상으로 고름
                conflict_name = clashing_names[0]
                for name in clashing_names:
                    idx = _find_player_index(players, name)
                    p = players[idx]
                    if p.line1 != p.line2:
                        conflict_name = name
                        break
                break

        if conflict_line is None:
            break

        key = conflict_name.strip().lower()
        avoid.setdefault(key, set()).add(conflict_line)
        current = assign_lines_with_pins(players, {}, hard_pinned=hard_pinned, avoid=avoid)

    return current


def solve_team_split(
    players: list[PlayerInfo],
    line_assignment: dict[int, str],
    constraints: Optional[list] = None,
    exclude_signature: Optional[frozenset] = None,
):
    """split_teams을 일반화한 버전. constraints로 'same'(같은 팀), 'diff'(다른 팀),
    'fixed'(특정 팀 고정) 조건을 걸 수 있고, exclude_signature로 특정 팀A 구성을 배제할 수 있다.
    조건을 만족하는 조합이 하나도 없으면 None을 반환한다."""
    constraints = constraints or []
    by_line: dict[str, list[int]] = {line: [] for line in LINES}
    for idx, line in line_assignment.items():
        by_line[line].append(idx)

    best = None
    for combo in itertools.product([0, 1], repeat=len(LINES)):
        side: dict[int, str] = {}
        valid_combo = True
        for line, choice in zip(LINES, combo):
            pair = by_line[line]
            if len(pair) != 2:
                valid_combo = False
                break
            a_idx, b_idx = (pair[0], pair[1]) if choice == 0 else (pair[1], pair[0])
            side[a_idx] = "A"
            side[b_idx] = "B"
        if not valid_combo:
            return None

        ok = True
        for kind, payload in constraints:
            if kind == "same":
                if len({side[i] for i in payload}) > 1:
                    ok = False
                    break
            elif kind == "diff":
                i, j = payload
                if side[i] == side[j]:
                    ok = False
                    break
            elif kind == "fixed":
                idx_, team_ = payload
                if side[idx_] != team_:
                    ok = False
                    break
        if not ok:
            continue

        a_set = frozenset(i for i, s in side.items() if s == "A")
        if exclude_signature is not None and a_set == exclude_signature:
            continue

        team_a = [(line_assignment[i], i) for i in side if side[i] == "A"]
        team_b = [(line_assignment[i], i) for i in side if side[i] == "B"]
        score_a = sum(players[i].score for _, i in team_a)
        score_b = sum(players[i].score for _, i in team_b)
        diff = abs(score_a - score_b)

        if best is None or diff < best[4]:
            best = (team_a, team_b, score_a, score_b, diff)

    if best is None:
        return None
    team_a, team_b, score_a, score_b, _ = best
    return team_a, team_b, score_a, score_b


# ---------------------------------------------------------------------------
# 봇 / 슬래시 명령어
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    bot.add_view(FillApplyView())
    bot.add_view(LineApplyView())
    bot.add_view(ResultView())
    if DEV_GUILD_ID:
        guild = discord.Object(id=int(DEV_GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"길드({DEV_GUILD_ID}) 즉시 동기화 완료: {len(synced)}개 명령어")
    else:
        synced = await bot.tree.sync()
        print(f"전역 동기화 완료: {len(synced)}개 명령어 (전역 동기화는 반영까지 최대 1시간 걸릴 수 있어요)")
    print(f"로그인 완료: {bot.user}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "이 명령어는 '서버 관리' 권한이 있는 사람만 사용할 수 있어요.", ephemeral=True
        )
        return

    print(f"[슬래시 명령어 에러] {error!r}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send("처리 중 문제가 생겼어요. 다시 시도해주세요.", ephemeral=True)
        else:
            await interaction.response.send_message("처리 중 문제가 생겼어요. 다시 시도해주세요.", ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_member_remove(member: discord.Member):
    channel_id = leave_log_channels.get(member.guild.id)
    if channel_id is None:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden):
            return
    try:
        await channel.send(f"👋 **{member.display_name}**님이 서버를 나갔어요.")
    except discord.Forbidden:
        pass


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return  # ! 로 시작하지만 이 봇 명령어가 아닌 일반 대화는 무시
    if hasattr(ctx.command, "on_error"):
        return  # 명령어 자체에 이미 error 핸들러가 있으면 거기서 처리됨
    print(f"[! 명령어 에러] {error!r}")
    try:
        await ctx.reply("처리 중 문제가 생겼어요. 다시 시도해주세요.")
    except discord.HTTPException:
        pass



# ---------------------------------------------------------------------------
# 내전모집 / 내전신청 / 내전팀 - 라인별로 실시간 모집받고, 다 차면 /내전팀으로 팀 나누기
# ---------------------------------------------------------------------------

# 채널 ID -> {"message_id": int, "slots": {line: [applicant_dict, ...]}}
# applicant_dict = {"name": str, "tier": str, "division": Optional[str], "lp": Optional[int]}
recruitment_sessions: dict[int, dict] = {}

# 채널 ID -> {"message_id": int, "players": [PlayerInfo,...10명], "line_assignment": {idx: line}, "team_of": {idx: "A"/"B"}}
# /내전팀 실행 시 생성되고, /내전확정 시 삭제됨. 신청할 때 적어낸 주라인/부라인 정보(PlayerInfo.line1/line2)를
# 그대로 들고 있어서, /내전다시섞기 등에서 원래 선호도를 다시 활용할 수 있음.
active_matches: dict[int, dict] = {}

# 길드 ID -> 로그를 보낼 채널 ID (봇 재시작하면 초기화됨)
log_channels: dict[int, int] = {}

# 길드 ID -> 서버 퇴장 로그를 보낼 채널 ID
leave_log_channels: dict[int, int] = {}

# 길드 ID -> 먹물코인 잔액 데이터를 저장할 채널 ID
wallet_channels: dict[int, int] = {}
# 길드 ID -> 그 채널에 올려둔 잔액 메시지 ID (캐시, 없으면 채널을 뒤져서 찾음)
wallet_message_ids: dict[int, int] = {}
# 길드 ID -> 그 서버의 지갑 읽기/계산/저장을 한 번에 하나씩만 처리하게 만드는 잠금
# (여러 게임/송금이 거의 동시에 정산되면서 서로 낡은 값을 덮어쓰는 걸 방지)
wallet_locks: dict[int, asyncio.Lock] = {}


def _get_wallet_lock(guild_id: int) -> asyncio.Lock:
    lock = wallet_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        wallet_locks[guild_id] = lock
    return lock


_WALLET_HEADER = "🐙 **먹물코인 잔액**"
_WALLET_FLOOR = 10_000  # 잔액은 이 아래로 절대 안 내려감 (등록 안 된 사람의 기본값이기도 함)
_TRANSFER_MIN = 1_000_000  # 송금은 이 금액부터 가능

# 길드 ID -> 팀 결과 + 결과입력 버튼을 올릴 채널 ID
result_channels: dict[int, int] = {}
# 길드 ID -> 누적 전적을 기록할 채널 ID
record_channels: dict[int, int] = {}
# 길드 ID -> 전적 채널에 올려둔 누적 전적 메시지 ID (캐시, 없으면 채널을 뒤져서 찾음)
record_message_ids: dict[int, int] = {}

_RECORD_HEADER = "📊 **내전 누적 전적**"


def _parse_team_names(content: str) -> tuple[list[str], list[str]]:
    """/내전팀 결과 메시지 형식에서 팀A/팀B 닉네임 목록을 뽑아낸다."""
    parts = content.split("**🔴 팀 B**")
    team_a_part = parts[0]
    team_b_part = parts[1] if len(parts) > 1 else ""
    pattern = re.compile(r"^- .+?: (.+?) \(", re.MULTILINE)
    return pattern.findall(team_a_part), pattern.findall(team_b_part)


# ---------------------------------------------------------------------------
# 먹물코인 지갑 (채널 메시지에 잔액을 저장/갱신하는 방식, 전적 기록과 같은 패턴)
# ---------------------------------------------------------------------------

async def _find_wallet_message(client: discord.Client, channel: discord.abc.Messageable, guild_id: int):
    cached_id = wallet_message_ids.get(guild_id)
    if cached_id is not None:
        try:
            return await channel.fetch_message(cached_id)
        except (discord.NotFound, discord.Forbidden):
            pass

    async for msg in channel.history(limit=50):
        if msg.author.id == client.user.id and msg.content.startswith(_WALLET_HEADER):
            wallet_message_ids[guild_id] = msg.id
            return msg

    return None


def _parse_wallet_text(content: str) -> dict[int, int]:
    balances: dict[int, int] = {}
    for line in content.splitlines():
        m = re.match(r"^<@!?(\d+)>: ([\d,]+)$", line.strip())
        if m:
            balances[int(m.group(1))] = int(m.group(2).replace(",", ""))
    return balances


def _build_wallet_text(balances: dict[int, int]) -> str:
    lines_out = [_WALLET_HEADER, ""]
    for uid, bal in sorted(balances.items(), key=lambda kv: -kv[1]):
        lines_out.append(f"<@{uid}>: {bal:,}")
    return "\n".join(lines_out)


async def _get_wallet_channel(guild_id: int, client: discord.Client):
    channel_id = wallet_channels.get(guild_id)
    if channel_id is None:
        return None
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden):
            return None
    return channel


async def _replace_wallet_message(
    client: discord.Client, channel: discord.abc.Messageable, guild_id: int, balances: dict[int, int]
) -> discord.Message:
    """새 잔액 메시지를 먼저 올리고, 성공하면 기존 메시지를 지운다 (전송 실패 시 데이터 유실 방지)."""
    old_msg = await _find_wallet_message(client, channel, guild_id)
    text = _build_wallet_text(balances)
    new_msg = await channel.send(text)
    wallet_message_ids[guild_id] = new_msg.id

    if old_msg is not None and old_msg.id != new_msg.id:
        try:
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    return new_msg


_BET_REFUND_RATIO = {
    "blackjack": lambda bet: bet + int(bet * 1.5),  # 3:2 지급
    "win": lambda bet: bet * 2,
    "push": lambda bet: bet,
    "lose": lambda bet: 0,
    "bust": lambda bet: 0,
    "same_win": lambda bet: bet * 11,  # 하이로우에서 '같다'를 정확히 맞히면 10:1 지급
}


async def _settle_bet(guild_id: int, user_id: int, bet: int, outcome: str):
    """베팅 결과를 지갑에 반영한다. (net 손익, 새 잔액) 튜플을 반환하거나,
    지갑 채널이 없거나 권한이 없으면 (None, 사유) 를 반환한다."""
    channel = await _get_wallet_channel(guild_id, bot)
    if channel is None:
        return None, "no_channel"

    async with _get_wallet_lock(guild_id):
        msg = await _find_wallet_message(bot, channel, guild_id)
        balances = _parse_wallet_text(msg.content) if msg else {}
        current = balances.get(user_id, _WALLET_FLOOR)
        refund = _BET_REFUND_RATIO[outcome](bet)
        new_balance = max(_WALLET_FLOOR, current + refund)
        balances[user_id] = new_balance

        try:
            await _replace_wallet_message(bot, channel, guild_id, balances)
        except discord.Forbidden:
            return None, "forbidden"

    return (refund - bet, new_balance), None



async def _find_record_message(client: discord.Client, channel: discord.abc.Messageable, guild_id: int):
    cached_id = record_message_ids.get(guild_id)
    if cached_id is not None:
        try:
            return await channel.fetch_message(cached_id)
        except (discord.NotFound, discord.Forbidden):
            pass

    async for msg in channel.history(limit=50):
        if msg.author.id == client.user.id and msg.content.startswith(_RECORD_HEADER):
            record_message_ids[guild_id] = msg.id
            return msg

    return None


def _parse_record_text(content: str) -> dict[str, tuple[int, int]]:
    records: dict[str, tuple[int, int]] = {}
    for line in content.splitlines():
        m = re.match(r"^(.+?): (\d+)승 (\d+)패$", line.strip())
        if m:
            records[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return records


def _build_record_text(records: dict[str, tuple[int, int]]) -> str:
    lines_out = [_RECORD_HEADER, ""]
    for name, (win, loss) in sorted(records.items(), key=lambda kv: (-kv[1][0], kv[0])):
        lines_out.append(f"{name}: {win}승 {loss}패")
    return "\n".join(lines_out)


async def _replace_record_message(
    client: discord.Client, channel: discord.abc.Messageable, guild_id: int, records: dict[str, tuple[int, int]]
) -> None:
    """새 내용으로 새 메시지를 먼저 올리고, 성공하면 기존 누적 전적 메시지를 지운다.
    (반대 순서로 하면 삭제는 성공했는데 전송이 실패했을 때 기록이 통째로 날아갈 수 있어서 이 순서를 씀)"""
    existing_msg = await _find_record_message(client, channel, guild_id)

    text = _build_record_text(records)
    new_msg = await channel.send(text)
    record_message_ids[guild_id] = new_msg.id

    if existing_msg is not None and existing_msg.id != new_msg.id:
        try:
            await existing_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass


async def _apply_record_updates(
    interaction: discord.Interaction, channel: discord.abc.Messageable, updates: dict[str, tuple[int, int]]
) -> None:
    guild_id = interaction.guild_id
    existing_msg = await _find_record_message(interaction.client, channel, guild_id)
    records = _parse_record_text(existing_msg.content) if existing_msg else {}

    for name, (win, loss) in updates.items():
        prev_win, prev_loss = records.get(name, (0, 0))
        records[name] = (prev_win + win, prev_loss + loss)

    await _replace_record_message(interaction.client, channel, guild_id, records)


async def _send_log(interaction: discord.Interaction, message: str) -> None:
    channel_id = log_channels.get(interaction.guild_id)
    if channel_id is None:
        return
    channel = interaction.client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden):
            return
    try:
        await channel.send(message)
    except discord.Forbidden:
        pass


def _build_recruitment_text(slots: dict[str, list[dict]]) -> str:
    lines_out = ["**⚔️ 내전 모집 중**", "아래 **신청하기** 버튼을 눌러서 라인/티어를 입력해주세요.", ""]
    total = 0
    for line in LINES:
        applicants = slots[line]
        total += len(applicants)
        filled = [f"{a['name']}, {tier_label(a['tier'], a['division'], a['lp'])}" for a in applicants]
        empty_count = 2 - len(applicants)
        slot_parts = filled + ["빈자리"] * empty_count
        lines_out.append(f"{line}: {len(applicants)}/2 - " + " / ".join(slot_parts))
    lines_out.append("")
    lines_out.append(f"총 {total}/10명" + (" — /내전팀 으로 팀을 나눠보세요!" if total == 10 else ""))
    return "\n".join(lines_out)


def _build_fill_text(waitlist: list[dict]) -> str:
    lines_out = ["**⚔️ 내전 대기자 모집 중**", "아래 **신청하기** 버튼을 눌러서 티어/라인을 입력해주세요.", ""]
    for i, a in enumerate(waitlist, start=1):
        label = tier_label(a["tier"], a["division"], a.get("lp"))
        lines_out.append(f"{i}. {a['name']} - {label} (주라인 {a['line1']} / 부라인 {a['line2']})")
    for i in range(len(waitlist) + 1, 11):
        lines_out.append(f"{i}. (빈자리)")
    lines_out.append("")
    total = len(waitlist)
    lines_out.append(f"총 {total}/10명" + (" — /내전팀 으로 팀을 나눠보세요!" if total == 10 else ""))
    return "\n".join(lines_out)


async def _modal_error_handler(interaction: discord.Interaction, error: Exception) -> None:
    print(f"[모달 에러] {error!r}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send("처리 중 문제가 생겼어요. 다시 시도해주세요.", ephemeral=True)
        else:
            await interaction.response.send_message("처리 중 문제가 생겼어요. 다시 시도해주세요.", ephemeral=True)
    except discord.HTTPException:
        pass


class SafeView(discord.ui.View):
    """버튼 콜백에서 예상 못한 예외가 나도 사용자에게 짧게 안내하고 콘솔에 로그를 남기는 View 베이스 클래스.
    (기존엔 각 View에 on_error가 없어서, 버그가 나면 사용자는 그냥 '상호작용 실패'만 보고 아무 설명도 못 받았음)"""

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        print(f"[View 에러] {item!r} -> {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("처리 중 문제가 생겼어요. 다시 시도해주세요.", ephemeral=True)
            else:
                await interaction.response.send_message("처리 중 문제가 생겼어요. 다시 시도해주세요.", ephemeral=True)
        except discord.HTTPException:
            pass


async def _handle_edit_forbidden(interaction: discord.Interaction) -> None:
    """모집판 메시지 수정이 권한 부족으로 실패했을 때, 어떤 권한이 없는지 로그 채널에 남기고
    행동한 사람에게도 짧게 알려준다."""
    channel = interaction.channel
    missing = []
    if interaction.guild is not None and channel is not None:
        perms = channel.permissions_for(interaction.guild.me)
        checks = [
            ("채널 보기", perms.view_channel),
            ("메시지 보내기", perms.send_messages),
            ("메시지 기록 보기", perms.read_message_history),
            ("링크 첨부", perms.embed_links),
        ]
        missing = [name for name, ok in checks if not ok]

    missing_text = ", ".join(missing) if missing else "알 수 없음 (권한 목록에서 확인 안 됨)"
    channel_mention = channel.mention if channel else "이 채널"
    await _send_log(
        interaction,
        f"⚠️ **모집판 갱신 실패** — {channel_mention}에서 봇에게 다음 권한이 없어요: {missing_text}",
    )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "⚠️ 처리는 됐지만, 봇 권한이 부족해서 모집판 화면은 갱신되지 않았어요. 관리자에게 봇 권한 확인을 요청해주세요.",
                ephemeral=True,
            )
    except discord.HTTPException:
        pass


class WaitlistApplyModal(discord.ui.Modal, title="내전 대기자 신청"):
    nickname_input = discord.ui.TextInput(
        label="롤 닉네임", placeholder="롤닉네임+태그를 정확하게 입력해주세요", required=True, max_length=30
    )
    tier_input = discord.ui.TextInput(
        label="티어 (예: 골드2, 다이아4, 마스터)", placeholder="최대티어를 적어주세요", required=True, max_length=10
    )
    line1_input = discord.ui.TextInput(label="주라인 (탑/정글/미드/원딜/서폿)", required=True, max_length=5)
    line2_input = discord.ui.TextInput(label="부라인 (탑/정글/미드/원딜/서폿)", required=True, max_length=5)
    lp_input = discord.ui.TextInput(
        label="LP (선택사항, 마스터 이상만 / 비워도 됨)", required=False, max_length=6
    )

    async def on_submit(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        session = recruitment_sessions.get(channel_id)
        if session is None or session.get("mode") != "fill":
            await interaction.response.send_message("이 채널엔 진행 중인 대기자 모집이 없어요.", ephemeral=True)
            return

        nickname = self.nickname_input.value.strip()
        if "(" in nickname or ")" in nickname:
            await interaction.response.send_message(
                "닉네임에는 괄호 `(` `)` 를 쓸 수 없어요. 다른 닉네임으로 다시 신청해주세요.", ephemeral=True
            )
            return
        line1_value = self.line1_input.value.strip()
        line2_value = self.line2_input.value.strip()
        lp_raw = self.lp_input.value.strip()

        tier_value, division_value, error = parse_tier_text(self.tier_input.value)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if line1_value not in LINES or line2_value not in LINES:
            await interaction.response.send_message(f"라인은 {'/'.join(LINES)} 중 하나여야 해요.", ephemeral=True)
            return

        lp_value = None
        if lp_raw:
            try:
                lp_value = int(lp_raw)
            except ValueError:
                await interaction.response.send_message("LP는 숫자로 입력해주세요.", ephemeral=True)
                return

        waitlist = session["waitlist"]
        nickname_key = nickname.lower()
        # 이미 신청했던 닉네임이면 정보 갱신 (덮어쓰기)
        already_in = any(a["name"].strip().lower() == nickname_key for a in waitlist)
        waitlist[:] = [a for a in waitlist if a["name"].strip().lower() != nickname_key]

        if not already_in and len(waitlist) >= 10:
            await interaction.response.send_message("이미 대기자가 10명이 다 찼어요.", ephemeral=True)
            return

        waitlist.append(
            {
                "name": nickname,
                "tier": tier_value,
                "division": division_value,
                "line1": line1_value,
                "line2": line2_value,
                "lp": lp_value,
            }
        )

        await interaction.response.send_message(f"{nickname}님 신청 완료! ({len(waitlist)}/10)", ephemeral=True)

        label = tier_label(tier_value, division_value, lp_value)
        await _send_log(
            interaction,
            f"📝 **[채우기 신청]** {interaction.user.mention} → 닉네임 `{nickname}` ({label}, 주라인 {line1_value}/부라인 {line2_value})",
        )

        text = _build_fill_text(waitlist)
        try:
            msg = await interaction.channel.fetch_message(session["message_id"])
            await msg.edit(content=text)
        except discord.NotFound:
            pass
        except discord.Forbidden:
            await _handle_edit_forbidden(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error_handler(interaction, error)


class ResultModal(discord.ui.Modal, title="내전 결과 입력"):
    score_input = discord.ui.TextInput(
        label="스코어 (예: 2승1패 — 첫 숫자가 팀A 승수)", required=True, max_length=10
    )

    def __init__(self, team_a: list[str], team_b: list[str], source_message: discord.Message):
        super().__init__()
        self.team_a = team_a
        self.team_b = team_b
        self.source_message = source_message

    async def on_submit(self, interaction: discord.Interaction):
        match = re.fullmatch(r"\s*(\d+)\s*승\s*(\d+)\s*패\s*", self.score_input.value)
        if not match:
            await interaction.response.send_message(
                "형식이 안 맞아요. '2승1패'처럼 입력해주세요.", ephemeral=True
            )
            return

        a_wins, b_wins = int(match.group(1)), int(match.group(2))

        record_channel_id = record_channels.get(interaction.guild_id)
        if record_channel_id is None:
            await interaction.response.send_message(
                "먼저 /내전전적채널설정으로 전적을 기록할 채널을 지정해주세요.", ephemeral=True
            )
            return

        channel = interaction.client.get_channel(record_channel_id)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(record_channel_id)
            except (discord.NotFound, discord.Forbidden):
                await interaction.response.send_message(
                    "전적 채널을 찾을 수 없어요. 채널이 삭제됐거나 봇 권한을 확인해주세요.", ephemeral=True
                )
                return

        updates: dict[str, tuple[int, int]] = {}
        for name in self.team_a:
            updates[name] = (a_wins, b_wins)
        for name in self.team_b:
            updates[name] = (b_wins, a_wins)

        # 전적 채널 갱신(메시지 조회+전송+삭제)이 3초를 넘길 수 있어서 먼저 defer로 응답을 확정해둠
        await interaction.response.defer(ephemeral=True)

        try:
            await _apply_record_updates(interaction, channel, updates)
        except discord.Forbidden:
            await interaction.followup.send(
                f"{channel.mention}에서 메시지를 읽거나 쓸 권한이 없어요. 봇 권한을 확인해주세요.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"결과({a_wins}승{b_wins}패)를 {channel.mention} 전적에 반영했어요.", ephemeral=True
        )

        try:
            await self.source_message.edit(
                content=self.source_message.content + f"\n\n**✅ 결과 입력 완료 ({a_wins}승{b_wins}패)**",
                view=None,
            )
        except (discord.NotFound, discord.Forbidden):
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error_handler(interaction, error)


class ResultView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🏆 결과 입력", style=discord.ButtonStyle.success, custom_id="lolbot_result_button")
    async def result_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.permissions.manage_guild:
            await interaction.response.send_message(
                "이 버튼은 '서버 관리' 권한이 있는 사람만 사용할 수 있어요.", ephemeral=True
            )
            return
        team_a, team_b = _parse_team_names(interaction.message.content)
        if not team_a or not team_b:
            await interaction.response.send_message("이 메시지에서 팀 정보를 읽지 못했어요.", ephemeral=True)
            return
        await interaction.response.send_modal(ResultModal(team_a, team_b, interaction.message))


def _build_match_debug_items(constraints: dict) -> list[tuple]:
    """(종류, 그 종류 리스트 내 인덱스 또는 키, 표시용 문구) 목록. /내전변수확인에 번호 붙여서 보여줄 때 씀."""
    items = []
    for i, group in enumerate(constraints.get("same_groups", [])):
        items.append(("same", i, f"{'·'.join(group)}: 같은 팀"))
    for i, pair in enumerate(constraints.get("diff_pairs", [])):
        items.append(("diff", i, f"{pair[0]}·{pair[1]}: 다른 팀"))
    for name, line in constraints.get("fixed_lines", {}).items():
        items.append(("fixed", name, f"{name}: {line} 고정"))
    return items


def _build_match_debug_text(match: dict) -> str:
    players = match["players"]
    line_assignment = match["line_assignment"]
    team_of = match["team_of"]
    line_order = {line: i for i, line in enumerate(LINES)}

    lines_out = ["**🔧 이 채널의 매치 메모리 상태**", ""]
    for team in ("A", "B"):
        lines_out.append(f"__팀 {team}__")
        members = [i for i in team_of if team_of[i] == team]
        members.sort(key=lambda i: line_order[line_assignment[i]])
        for i in members:
            p = players[i]
            lines_out.append(f"- {line_assignment[i]}: {p.name} ({p.tier_text}) [주:{p.line1}/부:{p.line2}]")
        lines_out.append("")

    items = _build_match_debug_items(match["constraints"])
    lines_out.append("**걸려있는 조건:**")
    if items:
        for i, (_, _, label) in enumerate(items, start=1):
            lines_out.append(f"{i}. {label}")
    else:
        lines_out.append("(없음)")
    lines_out.append("")
    lines_out.append(f"메시지 ID: {match['message_id']}")
    return "\n".join(lines_out)


class PlayerLineEditModal(discord.ui.Modal, title="선호 라인 수정"):
    line1_input = discord.ui.TextInput(label="주라인 (탑/정글/미드/원딜/서폿)", required=True, max_length=5)
    line2_input = discord.ui.TextInput(label="부라인 (탑/정글/미드/원딜/서폿)", required=True, max_length=5)

    def __init__(self, channel_id: int, player_index: int, debug_message: discord.Message, current_line1: str, current_line2: str):
        super().__init__()
        self.channel_id = channel_id
        self.player_index = player_index
        self.debug_message = debug_message
        self.line1_input.default = current_line1
        self.line2_input.default = current_line2

    async def on_submit(self, interaction: discord.Interaction):
        match = active_matches.get(self.channel_id)
        if match is None:
            await interaction.response.send_message("이미 매치 데이터가 없어요.", ephemeral=True)
            return

        l1 = self.line1_input.value.strip()
        l2 = self.line2_input.value.strip()
        if l1 not in LINES or l2 not in LINES:
            await interaction.response.send_message(f"라인은 {'/'.join(LINES)} 중 하나여야 해요.", ephemeral=True)
            return

        p = match["players"][self.player_index]
        match["players"][self.player_index] = PlayerInfo(
            name=p.name, tier=p.tier, division=p.division, line1=l1, line2=l2, lp=p.lp
        )

        try:
            await self.debug_message.edit(
                content=_build_match_debug_text(match), view=MatchDebugView(self.channel_id, match)
            )
        except (discord.NotFound, discord.Forbidden):
            pass

        await interaction.response.send_message(
            f"'{p.name}'의 선호 라인을 주라인:{l1} / 부라인:{l2}로 수정했어요.", ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error_handler(interaction, error)


class PlayerLineSelect(discord.ui.Select):
    def __init__(self, channel_id: int, match: dict):
        self.channel_id = channel_id
        players = match["players"]
        options = [
            discord.SelectOption(label=f"{p.name} (주:{p.line1}/부:{p.line2})", value=str(i))
            for i, p in enumerate(players)
        ][:25]
        super().__init__(placeholder="선호 라인을 수정할 사람 선택", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        match = active_matches.get(self.channel_id)
        if match is None:
            await interaction.response.send_message("이미 매치 데이터가 없어요.", ephemeral=True)
            return
        idx = int(self.values[0])
        p = match["players"][idx]
        await interaction.response.send_modal(
            PlayerLineEditModal(self.channel_id, idx, interaction.message, p.line1, p.line2)
        )


class MatchDebugView(SafeView):
    MAX_ITEM_BUTTONS = 20  # 선택 메뉴(0행) + 조건 버튼(1~4행)

    def __init__(self, channel_id: int, match: dict):
        super().__init__(timeout=300)
        self.channel_id = channel_id

        self.add_item(PlayerLineSelect(channel_id, match))

        items = _build_match_debug_items(match["constraints"])
        for i, (kind, key, _label) in enumerate(items[: self.MAX_ITEM_BUTTONS], start=1):
            row = 1 + (i - 1) // 5
            self.add_item(self._make_item_button(i, kind, key, row))

    def _make_item_button(self, number: int, kind: str, key, row: int) -> discord.ui.Button:
        button = discord.ui.Button(label=f"❌{number}", style=discord.ButtonStyle.secondary, row=row)

        async def callback(interaction: discord.Interaction):
            match = active_matches.get(self.channel_id)
            if match is None:
                await interaction.response.edit_message(content="이미 매치 데이터가 없어요.", view=None)
                return

            constraints = match["constraints"]
            if kind == "same" and 0 <= key < len(constraints["same_groups"]):
                constraints["same_groups"].pop(key)
            elif kind == "diff" and 0 <= key < len(constraints["diff_pairs"]):
                constraints["diff_pairs"].pop(key)
            elif kind == "fixed":
                constraints["fixed_lines"].pop(key, None)

            await interaction.response.edit_message(
                content=_build_match_debug_text(match), view=MatchDebugView(self.channel_id, match)
            )

        button.callback = callback
        return button


class CancelApplyModal(discord.ui.Modal, title="내전 신청 취소"):
    nickname_input = discord.ui.TextInput(
        label="롤 닉네임", placeholder="롤닉네임+태그를 정확하게 입력해주세요", required=True, max_length=30
    )

    async def on_submit(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        session = recruitment_sessions.get(channel_id)
        if session is None:
            await interaction.response.send_message("이 채널엔 진행 중인 내전 모집이 없어요.", ephemeral=True)
            return

        nickname = self.nickname_input.value.strip()
        nickname_key = nickname.lower()

        if session["mode"] == "line":
            slots = session["slots"]
            found_line = None
            for line, applicants in slots.items():
                if any(a["name"].strip().lower() == nickname_key for a in applicants):
                    found_line = line
                    break

            if found_line is None:
                await interaction.response.send_message(f"'{nickname}'(으)로 신청된 내역을 찾을 수 없어요.", ephemeral=True)
                return

            slots[found_line] = [a for a in slots[found_line] if a["name"].strip().lower() != nickname_key]
            await interaction.response.send_message(f"'{nickname}'님의 {found_line} 신청을 취소했어요.", ephemeral=True)
            text = _build_recruitment_text(slots)
        else:  # fill
            waitlist = session["waitlist"]
            before = len(waitlist)
            waitlist[:] = [a for a in waitlist if a["name"].strip().lower() != nickname_key]
            if len(waitlist) == before:
                await interaction.response.send_message(f"'{nickname}'(으)로 신청된 내역을 찾을 수 없어요.", ephemeral=True)
                return
            session.get("pinned", {}).pop(nickname_key, None)
            await interaction.response.send_message(f"'{nickname}'님의 신청을 취소했어요.", ephemeral=True)
            text = _build_fill_text(waitlist)

        await _send_log(interaction, f"❌ **[취소]** {interaction.user.mention} → 닉네임 `{nickname}`")

        try:
            msg = await interaction.channel.fetch_message(session["message_id"])
            await msg.edit(content=text)
        except discord.NotFound:
            pass
        except discord.Forbidden:
            await _handle_edit_forbidden(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error_handler(interaction, error)


class FillApplyView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📝 신청하기", style=discord.ButtonStyle.primary, custom_id="lolbot_fill_apply_button")
    async def apply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WaitlistApplyModal())

    @discord.ui.button(label="❌ 취소하기", style=discord.ButtonStyle.secondary, custom_id="lolbot_fill_cancel_button")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CancelApplyModal())


class LineApplyModal(discord.ui.Modal, title="내전 라인 신청"):
    nickname_input = discord.ui.TextInput(
        label="롤 닉네임", placeholder="롤닉네임+태그를 정확하게 입력해주세요", required=True, max_length=30
    )
    line_input = discord.ui.TextInput(label="라인 (탑/정글/미드/원딜/서폿)", required=True, max_length=5)
    tier_input = discord.ui.TextInput(
        label="티어 (예: 골드2, 다이아4, 마스터)", placeholder="최대티어를 적어주세요", required=True, max_length=10
    )
    lp_input = discord.ui.TextInput(
        label="LP (선택사항, 마스터 이상만 / 비워도 됨)", required=False, max_length=6
    )

    async def on_submit(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        session = recruitment_sessions.get(channel_id)
        if session is None or session.get("mode") != "line":
            await interaction.response.send_message("이 채널엔 진행 중인 라인선택 모집이 없어요.", ephemeral=True)
            return

        nickname = self.nickname_input.value.strip()
        if "(" in nickname or ")" in nickname:
            await interaction.response.send_message(
                "닉네임에는 괄호 `(` `)` 를 쓸 수 없어요. 다른 닉네임으로 다시 신청해주세요.", ephemeral=True
            )
            return
        line_value = self.line_input.value.strip()
        lp_raw = self.lp_input.value.strip()

        if line_value not in LINES:
            await interaction.response.send_message(f"라인은 {'/'.join(LINES)} 중 하나여야 해요.", ephemeral=True)
            return

        tier_value, division_value, error = parse_tier_text(self.tier_input.value)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        lp_value = None
        if lp_raw:
            try:
                lp_value = int(lp_raw)
            except ValueError:
                await interaction.response.send_message("LP는 숫자로 입력해주세요.", ephemeral=True)
                return

        slots = session["slots"]
        nickname_key = nickname.lower()

        # 이미 신청했던 닉네임이면 기존 자리에서 빼줌 (라인 변경 지원)
        for line, applicants in slots.items():
            slots[line] = [a for a in applicants if a["name"].strip().lower() != nickname_key]

        if len(slots[line_value]) >= 2:
            await interaction.response.send_message(f"{line_value} 라인은 이미 가득 찼어요 (2/2).", ephemeral=True)
            return

        slots[line_value].append(
            {"name": nickname, "tier": tier_value, "division": division_value, "lp": lp_value}
        )

        label = tier_label(tier_value, division_value, lp_value)
        await interaction.response.send_message(
            f"{nickname}님 {line_value}({label}) 신청 완료! ({len(slots[line_value])}/2)", ephemeral=True
        )

        await _send_log(
            interaction,
            f"📝 **[라인선택 신청]** {interaction.user.mention} → 닉네임 `{nickname}` ({label}, {line_value})",
        )

        text = _build_recruitment_text(slots)
        try:
            msg = await interaction.channel.fetch_message(session["message_id"])
            await msg.edit(content=text)
        except discord.NotFound:
            pass
        except discord.Forbidden:
            await _handle_edit_forbidden(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _modal_error_handler(interaction, error)


class LineApplyView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📝 신청하기", style=discord.ButtonStyle.primary, custom_id="lolbot_line_apply_button")
    async def apply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LineApplyModal())

    @discord.ui.button(label="❌ 취소하기", style=discord.ButtonStyle.secondary, custom_id="lolbot_line_cancel_button")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CancelApplyModal())


recruit_group = app_commands.Group(name="내전모집", description="내전 모집을 시작합니다.")


@recruit_group.command(name="라인선택", description="[관리자] 라인 자리(탑/정글/미드/원딜/서폿 각 2명)를 직접 선택해서 신청받습니다.")
@app_commands.checks.has_permissions(manage_guild=True)
async def start_recruitment_line(interaction: discord.Interaction):
    channel_id = interaction.channel_id

    if channel_id in recruitment_sessions:
        await interaction.response.send_message(
            "이미 이 채널에 진행 중인 내전 모집이 있어요. 새로 열려면 먼저 /내전모집취소로 끝내주세요.",
            ephemeral=True,
        )
        return

    slots = {line: [] for line in LINES}
    text = _build_recruitment_text(slots)
    await interaction.response.send_message(text, view=LineApplyView())
    msg = await interaction.original_response()

    recruitment_sessions[channel_id] = {"mode": "line", "message_id": msg.id, "slots": slots}


@recruit_group.command(name="채우기", description="[관리자] 대기자명단에 버튼으로 신청받는 방식으로 모집을 시작합니다.")
@app_commands.checks.has_permissions(manage_guild=True)
async def start_recruitment_fill(interaction: discord.Interaction):
    channel_id = interaction.channel_id

    if channel_id in recruitment_sessions:
        await interaction.response.send_message(
            "이미 이 채널에 진행 중인 내전 모집이 있어요. 새로 열려면 먼저 /내전모집취소로 끝내주세요.",
            ephemeral=True,
        )
        return

    waitlist: list[dict] = []
    text = _build_fill_text(waitlist)
    await interaction.response.send_message(text, view=FillApplyView())
    msg = await interaction.original_response()

    recruitment_sessions[channel_id] = {"mode": "fill", "message_id": msg.id, "waitlist": waitlist, "pinned": {}}


@bot.tree.command(name="내전팀", description="[관리자] 모집이 다 채워졌으면 팀을 나눕니다. (라인선택/채우기 모집 둘 다 지원)")
@app_commands.checks.has_permissions(manage_guild=True)
async def make_teams_from_recruitment(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    session = recruitment_sessions.get(channel_id)
    if session is None:
        await interaction.response.send_message(
            "이 채널엔 진행 중인 내전 모집이 없어요. 먼저 /내전모집으로 시작해주세요.", ephemeral=True
        )
        return

    if session["mode"] == "line":
        slots = session["slots"]
        total = sum(len(v) for v in slots.values())
        if total != 10:
            short = ", ".join(f"{line} {len(slots[line])}/2" for line in LINES if len(slots[line]) < 2)
            await interaction.response.send_message(
                f"아직 10명이 다 안 모였어요 (현재 {total}/10). 부족한 라인: {short}", ephemeral=True
            )
            return

        players = []
        line_assignment = {}
        idx = 0
        for line in LINES:
            for a in slots[line]:
                players.append(PlayerInfo(name=a["name"], tier=a["tier"], division=a["division"], line1=line, line2=line, lp=a["lp"]))
                line_assignment[idx] = line
                idx += 1
    else:  # fill
        waitlist = session["waitlist"]
        if len(waitlist) != 10:
            await interaction.response.send_message(
                f"아직 10명이 다 안 모였어요 (현재 {len(waitlist)}/10).", ephemeral=True
            )
            return

        players = [
            PlayerInfo(name=a["name"], tier=a["tier"], division=a["division"], line1=a["line1"], line2=a["line2"], lp=a.get("lp"))
            for a in waitlist
        ]
        pinned = session.get("pinned", {})
        line_assignment = assign_lines_with_pins(players, pinned)

    team_a, team_b, score_a, score_b = split_teams(players, line_assignment)
    result_text = format_result(players, team_a, team_b, score_a, score_b)
    await interaction.response.send_message(result_text)
    result_msg = await interaction.original_response()

    team_of: dict[int, str] = {}
    for _, i in team_a:
        team_of[i] = "A"
    for _, i in team_b:
        team_of[i] = "B"

    # /내전라인고정으로 실제 적용됐던 라인은, 팀 나눈 뒤에도 계속 고정된 걸로 취급함
    initial_fixed_lines: dict[str, str] = {}
    if session["mode"] == "fill":
        for name_lower, pinned_line in session.get("pinned", {}).items():
            for i, p in enumerate(players):
                if p.name.strip().lower() == name_lower and line_assignment[i] == pinned_line:
                    initial_fixed_lines[p.name] = pinned_line
                    break

    active_matches[channel_id] = {
        "message_id": result_msg.id,
        "players": players,
        "line_assignment": line_assignment,
        "team_of": team_of,
        "constraints": {"same_groups": [], "diff_pairs": [], "fixed_lines": initial_fixed_lines},
    }

    try:
        msg = await interaction.channel.fetch_message(session["message_id"])
        await msg.edit(content="**⚔️ 내전 모집 종료 — 팀 배정 완료!** (아래 결과 참고)", view=None)
    except discord.NotFound:
        pass
    except discord.Forbidden:
        await _handle_edit_forbidden(interaction)

    del recruitment_sessions[channel_id]


@bot.tree.command(name="모의내전테스트", description="[관리자] 테스트용 가짜 10명으로 모집 없이 바로 팀 결과를 만듭니다.")
@app_commands.checks.has_permissions(manage_guild=True)
async def make_test_match(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    if channel_id in active_matches:
        await interaction.response.send_message(
            "이미 이 채널에 조정 가능한 팀 결과가 있어요. 먼저 /내전확정으로 끝내거나 다른 채널에서 해주세요.",
            ephemeral=True,
        )
        return

    players = []
    for i in range(1, 11):
        tier = random.choice(TIERS)
        division = None if tier in _NO_DIVISION_SCORE else random.choice(DIVISIONS)
        line1, line2 = random.sample(LINES, 2)
        players.append(PlayerInfo(name=f"테스트{i}", tier=tier, division=division, line1=line1, line2=line2))

    line_assignment = assign_lines_with_pins(players, {})
    team_a, team_b, score_a, score_b = split_teams(players, line_assignment)
    result_text = "🧪 **테스트 매치** (실제 신청 없이 무작위로 생성됨)\n\n" + format_result(
        players, team_a, team_b, score_a, score_b
    )
    await interaction.response.send_message(result_text)
    result_msg = await interaction.original_response()

    team_of: dict[int, str] = {}
    for _, i in team_a:
        team_of[i] = "A"
    for _, i in team_b:
        team_of[i] = "B"

    active_matches[channel_id] = {
        "message_id": result_msg.id,
        "players": players,
        "line_assignment": line_assignment,
        "team_of": team_of,
        "constraints": {"same_groups": [], "diff_pairs": [], "fixed_lines": {}},
    }


# ---------------------------------------------------------------------------
# /내전팀 이후 팀 구성을 유연하게 조정하는 명령어들
# (active_matches 메모리에 저장된 이 채널의 매치 상태를 대상으로 하며, /내전확정 시 삭제됨)
# ---------------------------------------------------------------------------

_NO_MATCH_MSG = "이 채널에는 조정 가능한 팀 결과가 없어요 (아직 /내전팀을 안 했거나 이미 확정됨)."
_MSG_GONE = "팀 결과 메시지를 찾을 수 없어요 (삭제됐을 수 있어요). 다시 /내전팀을 실행해주세요."


async def _safe_edit_match_message(interaction: discord.Interaction, msg: discord.Message, text: str) -> bool:
    """팀 결과 메시지 수정을 시도하고, 실패하면 이유를 안내한 뒤 False를 반환한다."""
    try:
        await msg.edit(content=text)
        return True
    except discord.NotFound:
        await interaction.response.send_message("팀 결과 메시지를 찾을 수 없어요 (삭제됐을 수 있어요).", ephemeral=True)
        return False
    except discord.Forbidden:
        await interaction.response.send_message(
            "이 메시지를 수정할 권한이 없어요. 봇 권한을 확인해주세요.", ephemeral=True
        )
        return False


@bot.tree.command(name="내전라인교체", description="[관리자] 두 사람의 라인을 서로 바꾸는 조건을 기록합니다 (/내전다시섞기 실행 시 반영).")
@app_commands.describe(닉네임1="라인을 바꿀 사람 1", 닉네임2="라인을 바꿀 사람 2")
@app_commands.checks.has_permissions(manage_guild=True)
async def swap_lines(interaction: discord.Interaction, 닉네임1: str, 닉네임2: str):
    channel_id = interaction.channel_id
    match = active_matches.get(channel_id)
    if match is None:
        await interaction.response.send_message(_NO_MATCH_MSG, ephemeral=True)
        return

    players = match["players"]
    line_assignment = match["line_assignment"]

    idx_a = _find_player_index(players, 닉네임1)
    idx_b = _find_player_index(players, 닉네임2)
    if idx_a is None or idx_b is None:
        missing = 닉네임1 if idx_a is None else 닉네임2
        await interaction.response.send_message(f"'{missing}' 닉네임을 이 팀 결과에서 찾을 수 없어요.", ephemeral=True)
        return
    if line_assignment[idx_a] == line_assignment[idx_b]:
        await interaction.response.send_message("두 사람은 이미 같은 라인이에요.", ephemeral=True)
        return

    new_line_a = line_assignment[idx_b]
    new_line_b = line_assignment[idx_a]

    fixed_lines = dict(match["constraints"]["fixed_lines"])
    fixed_lines[players[idx_a].name] = new_line_a
    fixed_lines[players[idx_b].name] = new_line_b
    capacity_error = _validate_fixed_lines_capacity(fixed_lines)
    if capacity_error:
        await interaction.response.send_message(capacity_error, ephemeral=True)
        return

    match["constraints"]["fixed_lines"] = fixed_lines
    await interaction.response.send_message(
        f"'{닉네임1}'↔'{닉네임2}' 라인 교체를 기록했어요 ({닉네임1}→{new_line_a}, {닉네임2}→{new_line_b}). "
        "/내전다시섞기를 실행하면 반영돼요.",
        ephemeral=True,
    )


@bot.tree.command(name="내전팀묶기", description="[관리자] 지정한 사람들(2~5명)을 반드시 같은 팀으로 묶는 조건을 기록합니다 (/내전다시섞기 실행 시 반영).")
@app_commands.describe(
    닉네임1="묶을 사람 1", 닉네임2="묶을 사람 2", 닉네임3="묶을 사람 3(선택)",
    닉네임4="묶을 사람 4(선택)", 닉네임5="묶을 사람 5(선택)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def group_same_team(
    interaction: discord.Interaction,
    닉네임1: str,
    닉네임2: str,
    닉네임3: Optional[str] = None,
    닉네임4: Optional[str] = None,
    닉네임5: Optional[str] = None,
):
    names = [n for n in (닉네임1, 닉네임2, 닉네임3, 닉네임4, 닉네임5) if n is not None]

    channel_id = interaction.channel_id
    match = active_matches.get(channel_id)
    if match is None:
        await interaction.response.send_message(_NO_MATCH_MSG, ephemeral=True)
        return

    players = match["players"]
    line_assignment = match["line_assignment"]

    indices = []
    for name in names:
        idx = _find_player_index(players, name)
        if idx is None:
            await interaction.response.send_message(f"'{name}' 닉네임을 이 팀 결과에서 찾을 수 없어요.", ephemeral=True)
            return
        indices.append(idx)

    seen_lines: dict[str, int] = {}
    overlap_notes = []
    for idx in indices:
        line = line_assignment[idx]
        if line in seen_lines:
            other_idx = seen_lines[line]
            p1, p2 = players[idx], players[other_idx]
            if p1.line1 == p1.line2 == line and p2.line1 == p2.line2 == line:
                await interaction.response.send_message(
                    f"'{p1.name}'과(와) '{p2.name}'은(는) 둘 다 {line} 라인만 갈 수 있어서 같은 팀이 될 수 없어요.",
                    ephemeral=True,
                )
                return
            overlap_notes.append(f"{p1.name}·{p2.name}(현재 둘 다 {line})")
        seen_lines[line] = idx

    match["constraints"]["same_groups"].append([players[i].name for i in indices])
    note = ""
    if overlap_notes:
        note = f" (지금은 {', '.join(overlap_notes)}처럼 라인이 겹쳐있는데, /내전다시섞기 실행 시 가능하면 대안 라인으로 조정해서 맞춰볼게요.)"
    await interaction.response.send_message(
        f"{', '.join(names)}을(를) 같은 팀으로 묶는 조건을 기록했어요.{note} /내전다시섞기를 실행하면 반영돼요.",
        ephemeral=True,
    )


@bot.tree.command(name="내전팀분리", description="[관리자] 지정한 두 사람을 반드시 다른 팀으로 나누는 조건을 기록합니다 (/내전다시섞기 실행 시 반영).")
@app_commands.describe(닉네임1="분리할 사람 1", 닉네임2="분리할 사람 2")
@app_commands.checks.has_permissions(manage_guild=True)
async def separate_teams(interaction: discord.Interaction, 닉네임1: str, 닉네임2: str):
    channel_id = interaction.channel_id
    match = active_matches.get(channel_id)
    if match is None:
        await interaction.response.send_message(_NO_MATCH_MSG, ephemeral=True)
        return

    players = match["players"]

    idx_a = _find_player_index(players, 닉네임1)
    idx_b = _find_player_index(players, 닉네임2)
    if idx_a is None or idx_b is None:
        missing = 닉네임1 if idx_a is None else 닉네임2
        await interaction.response.send_message(f"'{missing}' 닉네임을 이 팀 결과에서 찾을 수 없어요.", ephemeral=True)
        return

    match["constraints"]["diff_pairs"].append((players[idx_a].name, players[idx_b].name))
    await interaction.response.send_message(
        f"'{닉네임1}', '{닉네임2}'를 다른 팀으로 나누는 조건을 기록했어요. /내전다시섞기를 실행하면 반영돼요.",
        ephemeral=True,
    )


@bot.tree.command(name="내전대타", description="[관리자] 한 명을 빼고 새 사람으로 즉시 교체합니다 (팀 재계산은 /내전다시섞기 실행 시 반영).")
@app_commands.describe(기존닉네임="빠질 사람", 새닉네임="새로 들어올 사람", 새티어="새 사람의 티어 (예: 골드2, 다이아4, 마스터)")
@app_commands.checks.has_permissions(manage_guild=True)
async def substitute_player(interaction: discord.Interaction, 기존닉네임: str, 새닉네임: str, 새티어: str):
    if "(" in 새닉네임 or ")" in 새닉네임:
        await interaction.response.send_message("닉네임에는 괄호 `(` `)` 를 쓸 수 없어요.", ephemeral=True)
        return

    channel_id = interaction.channel_id
    match = active_matches.get(channel_id)
    if match is None:
        await interaction.response.send_message(_NO_MATCH_MSG, ephemeral=True)
        return

    players = match["players"]
    line_assignment = match["line_assignment"]

    idx = _find_player_index(players, 기존닉네임)
    if idx is None:
        await interaction.response.send_message(f"'{기존닉네임}' 닉네임을 이 팀 결과에서 찾을 수 없어요.", ephemeral=True)
        return

    dup_idx = _find_player_index(players, 새닉네임)
    if dup_idx is not None and dup_idx != idx:
        await interaction.response.send_message(
            f"'{새닉네임}'은(는) 이미 이 팀 결과에 있는 닉네임이에요. 다른 닉네임을 써주세요.", ephemeral=True
        )
        return

    tier, division, error = parse_tier_text(새티어)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    old_line = line_assignment[idx]
    old_name = players[idx].name
    new_players = list(players)
    new_players[idx] = PlayerInfo(name=새닉네임, tier=tier, division=division, line1=old_line, line2=old_line, lp=None)
    match["players"] = new_players

    # 빠진 사람한테 걸려있던 조건들은 더 이상 의미가 없으니 정리함
    constraints = match["constraints"]
    constraints["same_groups"] = [
        [n for n in g if n.strip().lower() != old_name.strip().lower()] for g in constraints["same_groups"]
    ]
    constraints["same_groups"] = [g for g in constraints["same_groups"] if len(g) >= 2]
    constraints["diff_pairs"] = [
        (a, b) for a, b in constraints["diff_pairs"]
        if old_name.strip().lower() not in (a.strip().lower(), b.strip().lower())
    ]
    constraints["fixed_lines"] = {
        n: l for n, l in constraints["fixed_lines"].items() if n.strip().lower() != old_name.strip().lower()
    }

    await interaction.response.send_message(
        f"'{기존닉네임}' 대신 '{새닉네임}'을(를) 넣었어요 ({old_line} 자리). /내전다시섞기를 실행하면 팀에 반영돼요.",
        ephemeral=True,
    )


@bot.tree.command(name="내전라인요청", description="[관리자] 한 사람을 원하는 라인으로 옮기는 조건을 기록합니다 (/내전다시섞기 실행 시 반영).")
@app_commands.describe(닉네임="옮길 사람", 라인="원하는 라인")
@app_commands.choices(라인=[app_commands.Choice(name=l, value=l) for l in LINES])
@app_commands.checks.has_permissions(manage_guild=True)
async def request_line(interaction: discord.Interaction, 닉네임: str, 라인: app_commands.Choice[str]):
    channel_id = interaction.channel_id
    match = active_matches.get(channel_id)
    if match is None:
        await interaction.response.send_message(_NO_MATCH_MSG, ephemeral=True)
        return

    players = match["players"]
    line_assignment = match["line_assignment"]

    idx = _find_player_index(players, 닉네임)
    if idx is None:
        await interaction.response.send_message(f"'{닉네임}' 닉네임을 이 팀 결과에서 찾을 수 없어요.", ephemeral=True)
        return

    target_line = 라인.value
    if line_assignment[idx] == target_line and players[idx].name in match["constraints"]["fixed_lines"]:
        await interaction.response.send_message(f"'{닉네임}'은(는) 이미 {target_line}으로 고정되어 있어요.", ephemeral=True)
        return

    fixed_lines = dict(match["constraints"]["fixed_lines"])
    fixed_lines[players[idx].name] = target_line
    capacity_error = _validate_fixed_lines_capacity(fixed_lines)
    if capacity_error:
        await interaction.response.send_message(capacity_error, ephemeral=True)
        return

    match["constraints"]["fixed_lines"] = fixed_lines
    await interaction.response.send_message(
        f"'{닉네임}'을(를) {target_line}(으)로 옮기는 조건을 기록했어요. /내전다시섞기를 실행하면 반영돼요.",
        ephemeral=True,
    )


@bot.tree.command(name="내전다시섞기", description="[관리자] 지금까지 기록된 조건을 반영해서 팀을 다시 나눕니다 (선호도에 따라 라인도 바뀔 수 있음).")
@app_commands.checks.has_permissions(manage_guild=True)
async def reshuffle_teams(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    match = active_matches.get(channel_id)
    if match is None:
        await interaction.response.send_message(_NO_MATCH_MSG, ephemeral=True)
        return

    players = match["players"]
    line_assignment = match["line_assignment"]
    team_of = match["team_of"]
    current_a_set = frozenset(i for i, t in team_of.items() if t == "A")

    stored = match["constraints"]
    constraints = []
    for group in stored["same_groups"]:
        indices = [_find_player_index(players, n) for n in group]
        if len(indices) >= 2 and all(i is not None for i in indices):
            constraints.append(("same", indices))
    for name_a, name_b in stored["diff_pairs"]:
        ia = _find_player_index(players, name_a)
        ib = _find_player_index(players, name_b)
        if ia is not None and ib is not None:
            constraints.append(("diff", (ia, ib)))
    pinned = {name.strip().lower(): line for name, line in stored["fixed_lines"].items()}

    capacity_error = _validate_fixed_lines_capacity(stored["fixed_lines"])
    if capacity_error:
        await interaction.response.send_message(capacity_error, ephemeral=True)
        return

    # 1차 시도: 신청할 때 적어낸 주라인/부라인 선호도 + 지금까지 걸어둔 조건(팀묶기/팀분리/라인고정)을
    # 바탕으로 라인 배정부터 완전히 새로 계산. 팀묶기로 묶인 사람들이 같은 라인에 겹쳐서 구조적으로
    # 같은 팀이 될 수 없는 상태면, 대안 라인이 있는 쪽을 다른 라인으로 유도해서 풀어봄.
    fresh_line_assignment = assign_lines_with_pins(players, {}, hard_pinned=pinned)
    fresh_line_assignment = _resolve_same_group_line_clashes(
        players, fresh_line_assignment, pinned, stored["same_groups"]
    )
    result = None
    used_line_assignment = line_assignment
    if fresh_line_assignment != line_assignment:
        fresh_result = solve_team_split(players, fresh_line_assignment, constraints=constraints)
        if fresh_result is not None:
            fresh_a_set = frozenset(i for _, i in fresh_result[0])
            if fresh_line_assignment != line_assignment or fresh_a_set != current_a_set:
                result = fresh_result
                used_line_assignment = fresh_line_assignment

    # 2차 시도: 라인은 그대로 두고, 조건은 유지한 채 팀(A/B) 조합만 현재와 다르게
    if result is None:
        result = solve_team_split(players, line_assignment, constraints=constraints, exclude_signature=current_a_set)
        used_line_assignment = line_assignment

    if result is None:
        await interaction.response.send_message(
            "현재랑 다른 조합을 찾지 못했어요. 걸려있는 조건: " + _describe_constraints(stored), ephemeral=True
        )
        return

    team_a, team_b, score_a, score_b = result
    new_text = format_result(players, team_a, team_b, score_a, score_b)

    # 먼저 새 메시지를 올려보고, 성공했을 때만 이전 메시지를 지움 (실패해도 기존 결과가 안 사라지게)
    try:
        new_msg = await interaction.channel.send(new_text)
    except discord.Forbidden:
        await interaction.response.send_message(
            "새 팀 결과를 올릴 권한이 없어요. 봇 권한을 확인해주세요.", ephemeral=True
        )
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(f"팀 결과를 올리는 데 실패했어요: {e}", ephemeral=True)
        return

    old_msg = await _fetch_active_match_message(interaction, match)
    if old_msg is not None:
        try:
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    match["message_id"] = new_msg.id
    match["line_assignment"] = used_line_assignment
    match["team_of"] = {i: "A" for _, i in team_a} | {i: "B" for _, i in team_b}
    note = "" if used_line_assignment == line_assignment else " (신청할 때 적어낸 주라인/부라인을 다시 반영해서 라인도 조정됐어요)"
    await interaction.response.send_message(f"팀을 다른 조합으로 다시 나눴어요.{note}", ephemeral=True)


@bot.tree.command(name="내전위치교환", description="[관리자] 밸런스 재계산 없이 두 사람의 팀+라인을 그대로 맞바꿉니다.")
@app_commands.describe(닉네임1="교환할 사람 1", 닉네임2="교환할 사람 2")
@app_commands.checks.has_permissions(manage_guild=True)
async def swap_positions(interaction: discord.Interaction, 닉네임1: str, 닉네임2: str):
    channel_id = interaction.channel_id
    match = active_matches.get(channel_id)
    if match is None:
        await interaction.response.send_message(_NO_MATCH_MSG, ephemeral=True)
        return
    msg = await _fetch_active_match_message(interaction, match)
    if msg is None:
        del active_matches[channel_id]
        await interaction.response.send_message(_MSG_GONE, ephemeral=True)
        return

    players = match["players"]
    line_assignment = dict(match["line_assignment"])
    team_of = dict(match["team_of"])

    idx_a = _find_player_index(players, 닉네임1)
    idx_b = _find_player_index(players, 닉네임2)
    if idx_a is None or idx_b is None:
        missing = 닉네임1 if idx_a is None else 닉네임2
        await interaction.response.send_message(f"'{missing}' 닉네임을 이 팀 결과에서 찾을 수 없어요.", ephemeral=True)
        return

    line_assignment[idx_a], line_assignment[idx_b] = line_assignment[idx_b], line_assignment[idx_a]
    team_of[idx_a], team_of[idx_b] = team_of[idx_b], team_of[idx_a]

    team_a = [(line_assignment[i], i) for i in team_of if team_of[i] == "A"]
    team_b = [(line_assignment[i], i) for i in team_of if team_of[i] == "B"]
    score_a = sum(players[i].score for _, i in team_a)
    score_b = sum(players[i].score for _, i in team_b)

    if not await _safe_edit_match_message(interaction, msg, format_result(players, team_a, team_b, score_a, score_b)):
        return

    match["line_assignment"] = line_assignment
    match["team_of"] = team_of
    await interaction.response.send_message(
        f"'{닉네임1}', '{닉네임2}'의 자리를 그대로 맞바꿨어요 (밸런스 재계산 없음).", ephemeral=True
    )


@bot.tree.command(name="내전변수확인", description="[관리자] 이 채널에 저장된 매치 메모리 상태를 확인하고 선호 라인을 수정하거나 조건을 개별 삭제합니다.")
@app_commands.checks.has_permissions(manage_guild=True)
async def show_match_debug(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    match = active_matches.get(channel_id)
    if match is None:
        await interaction.response.send_message("이 채널엔 저장된 매치 메모리가 없어요.", ephemeral=True)
        return

    await interaction.response.send_message(
        _build_match_debug_text(match), view=MatchDebugView(channel_id, match), ephemeral=True
    )


@bot.tree.command(name="내전확정", description="[관리자] 현재 팀 결과를 확정합니다. 결과채널이 지정되어 있으면 그쪽으로 넘어갑니다.")
@app_commands.checks.has_permissions(manage_guild=True)
async def confirm_match(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    match = active_matches.get(channel_id)
    if match is None:
        await interaction.response.send_message("이 채널에서 확정할 팀 결과를 찾을 수 없어요.", ephemeral=True)
        return
    msg = await _fetch_active_match_message(interaction, match)
    if msg is None:
        del active_matches[channel_id]
        await interaction.response.send_message(_MSG_GONE, ephemeral=True)
        return

    result_channel_id = result_channels.get(interaction.guild_id)
    result_channel = None
    if result_channel_id is not None:
        try:
            result_channel = interaction.client.get_channel(result_channel_id)
            if result_channel is None:
                result_channel = await interaction.client.fetch_channel(result_channel_id)
            await result_channel.send(msg.content, view=ResultView())
        except Exception as e:
            print(f"[내전확정 결과채널 게시 에러] {e!r}")
            result_channel = None

    closing_note = "\n\n**✅ 확정됨**"
    try:
        await msg.edit(content=msg.content + closing_note)
    except (discord.NotFound, discord.Forbidden):
        pass

    del active_matches[channel_id]

    await interaction.response.send_message(
        "팀 결과를 확정했어요." + (f" {result_channel.mention}로 넘어갔어요." if result_channel else ""),
        ephemeral=True,
    )


@bot.tree.command(name="내전모집취소", description="[관리자] 진행 중인 내전 모집을 취소하고 종료합니다.")
@app_commands.checks.has_permissions(manage_guild=True)
async def end_recruitment(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    session = recruitment_sessions.get(channel_id)
    if session is None:
        await interaction.response.send_message("이 채널엔 진행 중인 내전 모집이 없어요.", ephemeral=True)
        return

    del recruitment_sessions[channel_id]

    await interaction.response.send_message("내전 모집을 종료했어요.")
    try:
        msg = await interaction.channel.fetch_message(session["message_id"])
        await msg.edit(content="**⚔️ 내전 모집이 취소되었습니다.**", view=None)
    except discord.NotFound:
        pass
    except discord.Forbidden:
        await _handle_edit_forbidden(interaction)


@bot.tree.command(name="내전로그설정", description="[관리자] 신청/취소 로그를 보낼 채널을 지정합니다.")
@app_commands.describe(채널="로그를 받을 채널")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_log_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    log_channels[interaction.guild_id] = 채널.id
    await interaction.response.send_message(f"이제부터 신청/취소 로그를 {채널.mention} 채널로 보낼게요.", ephemeral=True)


@bot.tree.command(name="서버퇴장로그설정", description="[관리자] 유저가 서버를 나가면 알려줄 채널을 지정합니다.")
@app_commands.describe(채널="퇴장 알림을 받을 채널")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_leave_log_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    leave_log_channels[interaction.guild_id] = 채널.id
    await interaction.response.send_message(f"이제부터 서버 퇴장 알림을 {채널.mention}에 보낼게요.", ephemeral=True)


@bot.tree.command(name="먹물코인채널설정", description="[관리자] 먹물코인 잔액 데이터를 저장할 채널을 지정합니다.")
@app_commands.describe(채널="먹물코인 데이터를 저장할 채널")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_wallet_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    wallet_channels[interaction.guild_id] = 채널.id
    wallet_message_ids.pop(interaction.guild_id, None)
    await interaction.response.send_message(f"이제부터 먹물코인 데이터를 {채널.mention}에 저장할게요.", ephemeral=True)


# ---------------------------------------------------------------------------
# 먹물코인 (! 접두사 명령어 — 내전 관련 / 명령어들과 구분)
# ---------------------------------------------------------------------------

@bot.command(name="먹물코인", aliases=["지갑", "잔액"])
async def check_wallet(ctx: commands.Context):
    if ctx.guild is None:
        return
    channel = await _get_wallet_channel(ctx.guild.id, bot)
    if channel is None:
        await ctx.reply("먼저 관리자가 `/먹물코인채널설정`으로 채널을 지정해야 해요.")
        return

    msg = await _find_wallet_message(bot, channel, ctx.guild.id)
    balances = _parse_wallet_text(msg.content) if msg else {}
    balance = balances.get(ctx.author.id, _WALLET_FLOOR)
    await ctx.reply(f"🐙 {ctx.author.mention}님의 먹물코인: **{balance:,}**")


@bot.command(name="송금")
async def transfer_coins(ctx: commands.Context, member: discord.Member, amount: int):
    if ctx.guild is None:
        return
    if member.id == ctx.author.id:
        await ctx.reply("자기 자신에게는 송금할 수 없어요.")
        return
    if member.bot:
        await ctx.reply("봇에게는 송금할 수 없어요.")
        return
    if amount < _TRANSFER_MIN:
        await ctx.reply(f"송금은 최소 **{_TRANSFER_MIN:,}** 먹물코인부터 가능해요.")
        return

    channel = await _get_wallet_channel(ctx.guild.id, bot)
    if channel is None:
        await ctx.reply("먼저 관리자가 `/먹물코인채널설정`으로 채널을 지정해야 해요.")
        return

    async with _get_wallet_lock(ctx.guild.id):
        msg = await _find_wallet_message(bot, channel, ctx.guild.id)
        balances = _parse_wallet_text(msg.content) if msg else {}
        sender_balance = balances.get(ctx.author.id, _WALLET_FLOOR)

        # 보내는 사람이 실제로 그만큼 갖고 있는지 먼저 확인 (10,000 하한선 자동 리필은
        # 게임 정산에서 잔액이 마이너스로 안 내려가게 하려는 용도이지, 없는 돈을 보내는 걸
        # 눈감아주려는 게 아님 — 이 검증이 없으면 잔액 10,000짜리 계정도 무한정 송금할 수 있었음)
        if amount > sender_balance:
            await ctx.reply(f"가진 먹물코인({sender_balance:,})보다 많이 보낼 수 없어요.")
            return

        new_sender_balance = sender_balance - amount
        refilled = new_sender_balance < _WALLET_FLOOR
        if refilled:
            new_sender_balance = _WALLET_FLOOR

        balances[ctx.author.id] = new_sender_balance
        balances[member.id] = balances.get(member.id, _WALLET_FLOOR) + amount

        try:
            await _replace_wallet_message(bot, channel, ctx.guild.id, balances)
        except discord.Forbidden:
            await ctx.reply(f"{channel.mention}에 메시지를 쓸 권한이 없어요. 봇 권한을 확인해주세요.")
            return

    note = f" (잔액이 {_WALLET_FLOOR:,} 밑으로 내려가서 {_WALLET_FLOOR:,}로 다시 채워졌어요)" if refilled else ""
    await ctx.reply(f"🐙 {ctx.author.mention} → {member.mention}에게 **{amount:,}** 먹물코인을 보냈어요.{note}")


@transfer_coins.error
async def transfer_coins_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MemberNotFound):
        await ctx.reply("그런 유저를 찾을 수 없어요. `!송금 @유저 1000000` 형식으로 적어주세요.")
    elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
        await ctx.reply("형식이 안 맞아요. `!송금 @유저 1000000` 이렇게 적어주세요.")
    else:
        print(f"[!송금 에러] {error!r}")
        await ctx.reply("처리 중 문제가 생겼어요. 다시 시도해주세요.")


# ---------------------------------------------------------------------------
# 블랙잭 (! 접두사 명령어)
# ---------------------------------------------------------------------------

_BJ_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
_BJ_SUITS = ["♠", "♥", "♦", "♣"]


def _bj_new_deck() -> list:
    deck = [(r, s) for r in _BJ_RANKS for s in _BJ_SUITS]
    random.shuffle(deck)
    return deck


def _bj_card_str(card: tuple) -> str:
    return f"{card[0]}{card[1]}"


def _bj_hand_value(hand: list) -> int:
    total = 0
    aces = 0
    for rank, _ in hand:
        if rank in ("J", "Q", "K"):
            total += 10
        elif rank == "A":
            total += 11
            aces += 1
        else:
            total += int(rank)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def _bj_is_blackjack(hand: list) -> bool:
    return len(hand) == 2 and _bj_hand_value(hand) == 21


_BJ_OUTCOME_TEXT = {
    "blackjack": "🂡 블랙잭! 승리 (3:2 지급)",
    "win": "🎉 승리",
    "lose": "😢 패배",
    "push": "🤝 무승부 (베팅액 환불)",
    "bust": "💥 버스트! 패배",
}


class BlackjackView(SafeView):
    def __init__(self, ctx: commands.Context, bet: int, deck: list, player_hand: list, dealer_hand: list):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.bet = bet
        self.deck = deck
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("이 블랙잭 판은 실행한 본인만 조작할 수 있어요.", ephemeral=True)
            return False
        return True

    def build_embed(self, *, reveal_dealer: bool, footer: Optional[str] = None) -> discord.Embed:
        embed = discord.Embed(title="🐙 먹물 블랙잭", color=discord.Color.dark_green())
        player_cards = " ".join(_bj_card_str(c) for c in self.player_hand)
        embed.add_field(
            name=f"{self.ctx.author.display_name} (플레이어)",
            value=f"{player_cards}\n합계: {_bj_hand_value(self.player_hand)}",
            inline=False,
        )

        if reveal_dealer:
            dealer_cards = " ".join(_bj_card_str(c) for c in self.dealer_hand)
            dealer_value = str(_bj_hand_value(self.dealer_hand))
        else:
            dealer_cards = f"{_bj_card_str(self.dealer_hand[0])} 🂠"
            dealer_value = "?"
        embed.add_field(name="딜러", value=f"{dealer_cards}\n합계: {dealer_value}", inline=False)

        embed.add_field(name="베팅액", value=f"{self.bet:,} 먹물코인", inline=False)
        if footer:
            embed.set_footer(text=footer)
        return embed

    async def _finish(self, interaction: discord.Interaction, outcome: str):
        for child in self.children:
            child.disabled = True
        self.stop()

        # 지갑 정산(디스코드 API 호출 여러 번)이 3초를 넘길 수 있어서 먼저 defer로 응답을 확정해둠
        await interaction.response.defer()

        settlement, error = await _settle_bet(self.ctx.guild.id, self.ctx.author.id, self.bet, outcome)
        if error == "forbidden":
            footer = f"{_BJ_OUTCOME_TEXT[outcome]} — ⚠️ 잔액 반영 실패 (봇 권한 확인 필요)"
        elif settlement is None:
            footer = f"{_BJ_OUTCOME_TEXT[outcome]} — ⚠️ 지갑 채널이 설정되지 않았어요"
        else:
            net, new_balance = settlement
            sign = "+" if net >= 0 else ""
            footer = f"{_BJ_OUTCOME_TEXT[outcome]} — {sign}{net:,} 먹물코인 (현재 잔액 {new_balance:,})"

        embed = self.build_embed(reveal_dealer=True, footer=footer)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="히트", style=discord.ButtonStyle.primary)
    async def hit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player_hand.append(self.deck.pop())
        if _bj_hand_value(self.player_hand) > 21:
            await self._finish(interaction, "bust")
            return
        await interaction.response.edit_message(embed=self.build_embed(reveal_dealer=False))

    @discord.ui.button(label="스테이", style=discord.ButtonStyle.secondary)
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        while _bj_hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        player_total = _bj_hand_value(self.player_hand)
        dealer_total = _bj_hand_value(self.dealer_hand)
        if dealer_total > 21 or player_total > dealer_total:
            outcome = "win"
        elif player_total < dealer_total:
            outcome = "lose"
        else:
            outcome = "push"
        await self._finish(interaction, outcome)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return

        settlement, _error = await _settle_bet(self.ctx.guild.id, self.ctx.author.id, self.bet, "lose")
        note = "⌛ 시간 초과로 😢 패배 처리됐어요. 베팅액이 몰수됐어요."
        if settlement is not None:
            _net, new_balance = settlement
            note += f" (현재 잔액 {new_balance:,})"

        try:
            embed = self.build_embed(reveal_dealer=True, footer=note)
            await self.message.edit(embed=embed, view=self)
        except (discord.NotFound, discord.HTTPException):
            pass


@bot.command(name="블랙잭")
async def blackjack(ctx: commands.Context, bet: int):
    if ctx.guild is None:
        return
    if bet <= 0:
        await ctx.reply("베팅액은 1 이상이어야 해요. 예: `!블랙잭 100000`")
        return

    channel = await _get_wallet_channel(ctx.guild.id, bot)
    if channel is None:
        await ctx.reply("먼저 관리자가 `/먹물코인채널설정`으로 채널을 지정해야 해요.")
        return

    async with _get_wallet_lock(ctx.guild.id):
        msg = await _find_wallet_message(bot, channel, ctx.guild.id)
        balances = _parse_wallet_text(msg.content) if msg else {}
        current = balances.get(ctx.author.id, _WALLET_FLOOR)

        if bet > current:
            await ctx.reply(f"가진 먹물코인({current:,})보다 많이 베팅할 수 없어요.")
            return

        # 베팅액을 먼저 차감
        balances[ctx.author.id] = current - bet
        try:
            await _replace_wallet_message(bot, channel, ctx.guild.id, balances)
        except discord.Forbidden:
            await ctx.reply(f"{channel.mention}에 메시지를 쓸 권한이 없어요. 봇 권한을 확인해주세요.")
            return

    deck = _bj_new_deck()
    player_hand = [deck.pop(), deck.pop()]
    dealer_hand = [deck.pop(), deck.pop()]
    view = BlackjackView(ctx, bet, deck, player_hand, dealer_hand)

    player_has_bj = _bj_is_blackjack(player_hand)
    dealer_has_bj = _bj_is_blackjack(dealer_hand)

    if player_has_bj or dealer_has_bj:
        for child in view.children:
            child.disabled = True
        view.stop()
        if player_has_bj and dealer_has_bj:
            outcome = "push"
        elif player_has_bj:
            outcome = "blackjack"
        else:
            outcome = "lose"  # 플레이어는 블랙잭이 아닌데 딜러만 내추럴 블랙잭인 경우 -> 히트/스테이 없이 즉시 패배

        settlement, error = await _settle_bet(ctx.guild.id, ctx.author.id, bet, outcome)
        outcome_text = _BJ_OUTCOME_TEXT[outcome]
        if outcome == "lose" and dealer_has_bj:
            outcome_text = "😢 딜러 블랙잭! 패배"
        if error == "forbidden":
            footer = f"{outcome_text} — ⚠️ 잔액 반영 실패 (봇 권한 확인 필요)"
        elif settlement is None:
            footer = f"{outcome_text}"
        else:
            net, new_balance = settlement
            sign = "+" if net >= 0 else ""
            footer = f"{outcome_text} — {sign}{net:,} 먹물코인 (현재 잔액 {new_balance:,})"

        embed = view.build_embed(reveal_dealer=True, footer=footer)
        await ctx.reply(embed=embed, view=view)
        return

    embed = view.build_embed(reveal_dealer=False)
    view.message = await ctx.reply(embed=embed, view=view)


@blackjack.error
async def blackjack_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
        await ctx.reply("베팅액을 숫자로 적어주세요. 예: `!블랙잭 100000`")
    else:
        print(f"[!블랙잭 에러] {error!r}")
        await ctx.reply("처리 중 문제가 생겼어요. 다시 시도해주세요.")


# ---------------------------------------------------------------------------
# 가위바위보 (! 접두사 명령어) - AI전 / 내기대전(PvP) 둘 다 지원
# ---------------------------------------------------------------------------

_RPS_BEATS = {"가위": "보", "바위": "가위", "보": "바위"}  # 키가 값을 이김


def _rps_judge_ai(player: str, ai: str) -> str:
    if player == ai:
        return "push"
    return "win" if _RPS_BEATS[player] == ai else "lose"


def _rps_judge_duel(a: str, b: str) -> str:
    """a, b 관점에서 승자를 'a'/'b'/'draw'로 반환."""
    if a == b:
        return "draw"
    return "a" if _RPS_BEATS[a] == b else "b"


class RPSAIChoiceView(SafeView):
    def __init__(self, ctx: commands.Context, bet: int):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.bet = bet
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("이 게임은 실행한 본인만 선택할 수 있어요.", ephemeral=True)
            return False
        return True

    async def _play(self, interaction: discord.Interaction, player_choice: str):
        for child in self.children:
            child.disabled = True
        self.stop()

        # 지갑 정산이 3초를 넘길 수 있어서 먼저 defer로 응답을 확정해둠
        await interaction.response.defer()

        ai_choice = random.choice(["가위", "바위", "보"])
        outcome = _rps_judge_ai(player_choice, ai_choice)

        settlement, error = await _settle_bet(self.ctx.guild.id, self.ctx.author.id, self.bet, outcome)
        if error == "forbidden":
            note = "⚠️ 잔액 반영 실패 (봇 권한 확인 필요)"
        elif settlement is None:
            note = "⚠️ 지갑 채널이 설정되지 않았어요"
        else:
            net, new_balance = settlement
            sign = "+" if net >= 0 else ""
            result_text = {"win": "🎉 승리!", "lose": "😢 패배", "push": "🤝 무승부"}[outcome]
            note = f"{result_text} — {sign}{net:,} 먹물코인 (현재 잔액 {new_balance:,})"

        embed = discord.Embed(title="🐙 먹물 가위바위보 (AI전)", color=discord.Color.blue())
        embed.add_field(name=self.ctx.author.display_name, value=player_choice, inline=True)
        embed.add_field(name="AI", value=ai_choice, inline=True)
        embed.add_field(name="베팅액", value=f"{self.bet:,} 먹물코인", inline=False)
        embed.set_footer(text=note)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="✌️ 가위", style=discord.ButtonStyle.primary)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play(interaction, "가위")

    @discord.ui.button(label="✊ 바위", style=discord.ButtonStyle.primary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play(interaction, "바위")

    @discord.ui.button(label="🖐️ 보", style=discord.ButtonStyle.primary)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play(interaction, "보")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return
        # AI전은 베팅액이 이미 차감된 상태이므로, 시간 초과 시 패배 처리와 동일하게 몰수
        settlement, _error = await _settle_bet(self.ctx.guild.id, self.ctx.author.id, self.bet, "lose")
        note = "⌛ 시간 초과로 😢 패배 처리됐어요. 베팅액이 몰수됐어요."
        if settlement is not None:
            _net, new_balance = settlement
            note += f" (현재 잔액 {new_balance:,})"
        try:
            embed = discord.Embed(title="🐙 먹물 가위바위보 (AI전)", color=discord.Color.blue())
            embed.add_field(name="베팅액", value=f"{self.bet:,} 먹물코인", inline=False)
            embed.set_footer(text=note)
            await self.message.edit(embed=embed, view=self)
        except (discord.NotFound, discord.HTTPException):
            pass


class RPSDuelView(SafeView):
    def __init__(self, ctx: commands.Context, challenger: discord.Member, bet: int):
        super().__init__(timeout=90)
        self.ctx = ctx
        self.player_a = ctx.author
        self.player_b = challenger
        self.bet = bet
        self.choices: dict[int, str] = {}
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.player_a.id, self.player_b.id):
            await interaction.response.send_message("이 대결의 당사자만 선택할 수 있어요.", ephemeral=True)
            return False
        if interaction.user.id in self.choices:
            await interaction.response.send_message("이미 선택했어요. 상대를 기다려주세요.", ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🐙 먹물 가위바위보 대결", color=discord.Color.orange())
        status_a = "✅ 선택 완료" if self.player_a.id in self.choices else "⏳ 대기 중"
        status_b = "✅ 선택 완료" if self.player_b.id in self.choices else "⏳ 대기 중"
        embed.add_field(name=self.player_a.display_name, value=status_a, inline=True)
        embed.add_field(name=self.player_b.display_name, value=status_b, inline=True)
        embed.add_field(name="베팅액", value=f"{self.bet:,} 먹물코인 (각자, 이긴 사람이 전부 획득)", inline=False)
        return embed

    async def _choose(self, interaction: discord.Interaction, choice: str):
        self.choices[interaction.user.id] = choice
        await interaction.response.send_message(f"'{choice}'를 선택했어요! 상대가 고를 때까지 기다려주세요.", ephemeral=True)

        if len(self.choices) < 2:
            if self.message is not None:
                try:
                    await self.message.edit(embed=self.build_embed())
                except discord.HTTPException:
                    pass
            return

        for child in self.children:
            child.disabled = True
        self.stop()

        a_choice = self.choices[self.player_a.id]
        b_choice = self.choices[self.player_b.id]
        result = _rps_judge_duel(a_choice, b_choice)

        channel = await _get_wallet_channel(self.ctx.guild.id, bot)
        note = "⚠️ 지갑 채널이 설정되지 않았어요"
        if channel is not None:
            try:
                async with _get_wallet_lock(self.ctx.guild.id):
                    msg = await _find_wallet_message(bot, channel, self.ctx.guild.id)
                    balances = _parse_wallet_text(msg.content) if msg else {}
                    if result == "draw":
                        balances[self.player_a.id] = max(_WALLET_FLOOR, balances.get(self.player_a.id, _WALLET_FLOOR) + self.bet)
                        balances[self.player_b.id] = max(_WALLET_FLOOR, balances.get(self.player_b.id, _WALLET_FLOOR) + self.bet)
                        note = "🤝 무승부! 베팅액 각자 환불됐어요."
                    else:
                        winner = self.player_a if result == "a" else self.player_b
                        loser = self.player_b if result == "a" else self.player_a
                        balances[winner.id] = max(_WALLET_FLOOR, balances.get(winner.id, _WALLET_FLOOR) + self.bet * 2)
                        balances[loser.id] = max(_WALLET_FLOOR, balances.get(loser.id, _WALLET_FLOOR))
                        note = f"🏆 {winner.mention} 승리! **{self.bet * 2:,}** 먹물코인을 가져갔어요."
                    await _replace_wallet_message(bot, channel, self.ctx.guild.id, balances)
            except discord.Forbidden:
                note = "⚠️ 잔액 반영 실패 (봇 권한 확인 필요)"

        embed = discord.Embed(title="🐙 먹물 가위바위보 대결 결과", color=discord.Color.gold())
        embed.add_field(name=self.player_a.display_name, value=a_choice, inline=True)
        embed.add_field(name=self.player_b.display_name, value=b_choice, inline=True)
        embed.add_field(name="결과", value=note, inline=False)

        if self.message is not None:
            try:
                await self.message.edit(content=None, embed=embed, view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="✌️ 가위", style=discord.ButtonStyle.primary)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "가위")

    @discord.ui.button(label="✊ 바위", style=discord.ButtonStyle.primary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "바위")

    @discord.ui.button(label="🖐️ 보", style=discord.ButtonStyle.primary)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "보")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return

        # 둘 중 하나라도 안 골랐으면 공정하게 베팅액을 각자 환불
        channel = await _get_wallet_channel(self.ctx.guild.id, bot)
        if channel is not None:
            try:
                async with _get_wallet_lock(self.ctx.guild.id):
                    msg = await _find_wallet_message(bot, channel, self.ctx.guild.id)
                    balances = _parse_wallet_text(msg.content) if msg else {}
                    balances[self.player_a.id] = max(_WALLET_FLOOR, balances.get(self.player_a.id, _WALLET_FLOOR) + self.bet)
                    balances[self.player_b.id] = max(_WALLET_FLOOR, balances.get(self.player_b.id, _WALLET_FLOOR) + self.bet)
                    await _replace_wallet_message(bot, channel, self.ctx.guild.id, balances)
            except discord.Forbidden:
                pass

        try:
            await self.message.edit(content="⌛ 시간 초과 — 대결이 취소되고 베팅액이 각자 환불됐어요.", embed=None, view=self)
        except (discord.NotFound, discord.HTTPException):
            pass


class RPSChallengeView(SafeView):
    def __init__(self, ctx: commands.Context, bet: int):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.bet = bet
        self.message: Optional[discord.Message] = None
        self.accepted = False  # 여러 명이 거의 동시에 도전 버튼을 눌러도 딱 한 명만 성사되게 막는 잠금

    @discord.ui.button(label="⚔️ 도전하기", style=discord.ButtonStyle.danger)
    async def challenge_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.accepted:
            await interaction.response.send_message(
                "아쉽지만 다른 사람이 먼저 도전해서 이 대결은 이미 마감됐어요 (돈은 안 빠져나갔어요).", ephemeral=True
            )
            return
        if interaction.user.id == self.ctx.author.id:
            await interaction.response.send_message("본인 게임에는 본인이 도전할 수 없어요.", ephemeral=True)
            return
        if interaction.user.bot:
            await interaction.response.send_message("봇은 도전할 수 없어요.", ephemeral=True)
            return

        # 이 지점부터 await 전까지는 동기 코드라, 거의 동시에 눌러도 딱 한 클릭만 여기를 통과함
        self.accepted = True

        # 아래 지갑 조회/차감 과정이 3초(상호작용 응답 제한)를 넘길 수 있어서, 무거운 작업 전에
        # 먼저 defer로 응답을 확정해둠. 이후 오류 안내는 followup으로, 최종 화면 갱신은
        # edit_original_response로 처리함 (edit_message는 defer 이후엔 쓸 수 없음)
        await interaction.response.defer()

        channel = await _get_wallet_channel(self.ctx.guild.id, bot)
        if channel is None:
            self.accepted = False  # 실패했으니 잠금 풀어서 다른 사람이 다시 도전할 수 있게
            await interaction.followup.send("먼저 관리자가 `/먹물코인채널설정`으로 채널을 지정해야 해요.", ephemeral=True)
            return

        # 실행자 베팅액은 /가위바위보 실행 시점에 이미 차감돼있으므로, 여기서는 도전자 몫만 확인/차감함
        async with _get_wallet_lock(self.ctx.guild.id):
            msg = await _find_wallet_message(bot, channel, self.ctx.guild.id)
            balances = _parse_wallet_text(msg.content) if msg else {}
            challenger_balance = balances.get(interaction.user.id, _WALLET_FLOOR)

            if challenger_balance < self.bet:
                self.accepted = False
                await interaction.followup.send(
                    f"이 베팅액({self.bet:,})을 감당할 잔액이 없어요 (현재 {challenger_balance:,}).", ephemeral=True
                )
                return

            balances[interaction.user.id] = challenger_balance - self.bet
            try:
                await _replace_wallet_message(bot, channel, self.ctx.guild.id, balances)
            except discord.Forbidden:
                self.accepted = False
                await interaction.followup.send("지갑 채널에 쓸 권한이 없어요. 봇 권한을 확인해주세요.", ephemeral=True)
                return

        for child in self.children:
            child.disabled = True
        self.stop()

        duel_view = RPSDuelView(self.ctx, interaction.user, self.bet)
        content = f"⚔️ {self.ctx.author.mention} vs {interaction.user.mention} — 베팅액 각자 {self.bet:,} 먹물코인 차감 완료!"
        await interaction.edit_original_response(content=content, embed=duel_view.build_embed(), view=duel_view)
        duel_view.message = await interaction.original_response()

    @discord.ui.button(label="❌ 취소", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("이 취소 버튼은 실행한 본인만 누를 수 있어요.", ephemeral=True)
            return
        if self.accepted:
            await interaction.response.send_message("이미 도전자가 정해져서 취소할 수 없어요.", ephemeral=True)
            return

        self.accepted = True  # 이 시점부터 도전 못 받게 잠금 (취소 처리 중 도전이 끼어드는 것 방지)
        for child in self.children:
            child.disabled = True
        self.stop()

        # 지갑 환불 처리가 3초를 넘길 수 있어서 먼저 defer로 응답을 확정해둠
        await interaction.response.defer()

        settlement, _error = await _settle_bet(self.ctx.guild.id, self.ctx.author.id, self.bet, "push")
        note = "❌ 실행자가 대결을 취소했어요. 베팅액을 환불했어요."
        if settlement is not None:
            _net, new_balance = settlement
            note += f" (현재 잔액 {new_balance:,})"
        await interaction.edit_original_response(content=note, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return
        # 도전자가 안 나타났으니, /가위바위보 실행 시 차감했던 실행자 베팅액을 환불
        settlement, _error = await _settle_bet(self.ctx.guild.id, self.ctx.author.id, self.bet, "push")
        note = "⌛ 도전자가 나타나지 않아 대결이 취소됐어요. 베팅액을 환불했어요."
        if settlement is not None:
            _net, new_balance = settlement
            note += f" (현재 잔액 {new_balance:,})"
        try:
            await self.message.edit(content=note, view=self)
        except (discord.NotFound, discord.HTTPException):
            pass


class RPSStartView(SafeView):
    def __init__(self, ctx: commands.Context, bet: int):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.bet = bet
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("이 게임은 실행한 본인만 선택할 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🤖 AI전", style=discord.ButtonStyle.primary)
    async def ai_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        self.stop()

        choice_view = RPSAIChoiceView(self.ctx, self.bet)
        embed = discord.Embed(
            title="🐙 먹물 가위바위보 (AI전)", description="가위/바위/보 중 하나를 선택하세요!", color=discord.Color.blue()
        )
        embed.add_field(name="베팅액", value=f"{self.bet:,} 먹물코인", inline=False)
        await interaction.response.edit_message(content=None, embed=embed, view=choice_view)
        choice_view.message = await interaction.original_response()

    @discord.ui.button(label="⚔️ 내기대전", style=discord.ButtonStyle.danger)
    async def pvp_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        self.stop()

        challenge_view = RPSChallengeView(self.ctx, self.bet)
        content = (
            f"⚔️ {self.ctx.author.mention}님이 **{self.bet:,}** 먹물코인 가위바위보 대결을 신청했어요! "
            "도전하려면 아래 버튼을 눌러주세요 (본인 제외 누구나 가능)."
        )
        await interaction.response.edit_message(content=content, embed=None, view=challenge_view)
        challenge_view.message = await interaction.original_response()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return

        # 진행 방식조차 못 골랐으니 아까 차감한 베팅액은 그대로 환불
        settlement, _error = await _settle_bet(self.ctx.guild.id, self.ctx.author.id, self.bet, "push")
        note = "⌛ 시간 초과로 취소됐어요. 베팅액을 환불했어요."
        if settlement is not None:
            _net, new_balance = settlement
            note += f" (현재 잔액 {new_balance:,})"
        try:
            await self.message.edit(content=note, view=self)
        except (discord.NotFound, discord.HTTPException):
            pass


@bot.command(name="가위바위보")
async def rps(ctx: commands.Context, bet: int):
    if ctx.guild is None:
        return
    if bet <= 0:
        await ctx.reply("베팅액은 1 이상이어야 해요. 예: `!가위바위보 100000`")
        return

    channel = await _get_wallet_channel(ctx.guild.id, bot)
    if channel is None:
        await ctx.reply("먼저 관리자가 `/먹물코인채널설정`으로 채널을 지정해야 해요.")
        return

    # 실행자 베팅액을 이 시점에 바로 차감 (AI전/내기대전 어느 쪽으로 가든 동일하게 적용,
    # 도전자와 거의 동시에 처리되면서 실행자 잔액이 이중으로 깎이는 걸 방지)
    async with _get_wallet_lock(ctx.guild.id):
        msg = await _find_wallet_message(bot, channel, ctx.guild.id)
        balances = _parse_wallet_text(msg.content) if msg else {}
        current = balances.get(ctx.author.id, _WALLET_FLOOR)
        if bet > current:
            await ctx.reply(f"가진 먹물코인({current:,})보다 많이 베팅할 수 없어요.")
            return

        balances[ctx.author.id] = current - bet
        try:
            await _replace_wallet_message(bot, channel, ctx.guild.id, balances)
        except discord.Forbidden:
            await ctx.reply(f"{channel.mention}에 메시지를 쓸 권한이 없어요. 봇 권한을 확인해주세요.")
            return

    view = RPSStartView(ctx, bet)
    view.message = await ctx.reply(
        f"🐙 {ctx.author.mention}님, 어떻게 진행할까요? (베팅액 **{bet:,}**, 이미 차감됨)", view=view
    )


@rps.error
async def rps_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
        await ctx.reply("베팅액을 숫자로 적어주세요. 예: `!가위바위보 100000`")
    else:
        print(f"[!가위바위보 에러] {error!r}")
        await ctx.reply("처리 중 문제가 생겼어요. 다시 시도해주세요.")


# ---------------------------------------------------------------------------
# 하이로우 (! 접두사 명령어) - 다음 카드가 더 높을지/낮을지/같을지 맞히는 1클릭 게임
# ---------------------------------------------------------------------------

_HILO_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
_HILO_SUITS = ["♠", "♥", "♦", "♣"]
_HILO_RANK_VALUE = {r: i + 1 for i, r in enumerate(_HILO_RANKS)}  # A=1 ... K=13

_HILO_OUTCOME_TEXT = {
    "win": "🎉 정답!",
    "same_win": "🎯 정확히 맞혔어요! (같은 숫자)",
    "push": "🤝 숫자가 같아서 무승부 처리 (베팅액 환불)",
    "lose": "😢 틀렸어요",
}


def _hilo_draw_card() -> tuple:
    return (random.choice(_HILO_RANKS), random.choice(_HILO_SUITS))


def _hilo_card_str(card: tuple) -> str:
    return f"{card[0]}{card[1]}"


class HiLoView(SafeView):
    def __init__(self, ctx: commands.Context, bet: int, first_card: tuple):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.bet = bet
        self.first_card = first_card
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("이 게임은 실행한 본인만 조작할 수 있어요.", ephemeral=True)
            return False
        return True

    def _base_embed(self, *, second_card: Optional[tuple] = None, footer: Optional[str] = None) -> discord.Embed:
        embed = discord.Embed(title="🐙 먹물 하이로우", color=discord.Color.purple())
        embed.add_field(name="첫 카드", value=_hilo_card_str(self.first_card), inline=True)
        if second_card is not None:
            embed.add_field(name="다음 카드", value=_hilo_card_str(second_card), inline=True)
        embed.add_field(name="베팅액", value=f"{self.bet:,} 먹물코인", inline=False)
        if footer:
            embed.set_footer(text=footer)
        return embed

    async def _play(self, interaction: discord.Interaction, guess: str):
        for child in self.children:
            child.disabled = True
        self.stop()

        # 지갑 정산이 3초를 넘길 수 있어서 먼저 defer로 응답을 확정해둠
        await interaction.response.defer()

        second_card = _hilo_draw_card()
        first_val = _HILO_RANK_VALUE[self.first_card[0]]
        second_val = _HILO_RANK_VALUE[second_card[0]]

        if second_val == first_val:
            actual = "same"
        elif second_val > first_val:
            actual = "high"
        else:
            actual = "low"

        if guess == "same":
            outcome = "same_win" if actual == "same" else "lose"
        elif actual == "same":
            outcome = "push"
        elif guess == actual:
            outcome = "win"
        else:
            outcome = "lose"

        settlement, error = await _settle_bet(self.ctx.guild.id, self.ctx.author.id, self.bet, outcome)
        if error == "forbidden":
            note = "⚠️ 잔액 반영 실패 (봇 권한 확인 필요)"
        elif settlement is None:
            note = "⚠️ 지갑 채널이 설정되지 않았어요"
        else:
            net, new_balance = settlement
            sign = "+" if net >= 0 else ""
            note = f"{_HILO_OUTCOME_TEXT[outcome]} — {sign}{net:,} 먹물코인 (현재 잔액 {new_balance:,})"

        embed = self._base_embed(second_card=second_card, footer=note)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="⬆️ 높다", style=discord.ButtonStyle.success)
    async def higher(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play(interaction, "high")

    @discord.ui.button(label="⬇️ 낮다", style=discord.ButtonStyle.danger)
    async def lower(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play(interaction, "low")

    @discord.ui.button(label="🟰 같다 (10:1)", style=discord.ButtonStyle.secondary)
    async def same(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play(interaction, "same")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return
        settlement, _error = await _settle_bet(self.ctx.guild.id, self.ctx.author.id, self.bet, "lose")
        note = "⌛ 시간 초과로 😢 패배 처리됐어요. 베팅액이 몰수됐어요."
        if settlement is not None:
            _net, new_balance = settlement
            note += f" (현재 잔액 {new_balance:,})"
        try:
            await self.message.edit(embed=self._base_embed(footer=note), view=self)
        except (discord.NotFound, discord.HTTPException):
            pass


@bot.command(name="하이로우")
async def hilo(ctx: commands.Context, bet: int):
    if ctx.guild is None:
        return
    if bet <= 0:
        await ctx.reply("베팅액은 1 이상이어야 해요. 예: `!하이로우 100000`")
        return

    channel = await _get_wallet_channel(ctx.guild.id, bot)
    if channel is None:
        await ctx.reply("먼저 관리자가 `/먹물코인채널설정`으로 채널을 지정해야 해요.")
        return

    async with _get_wallet_lock(ctx.guild.id):
        msg = await _find_wallet_message(bot, channel, ctx.guild.id)
        balances = _parse_wallet_text(msg.content) if msg else {}
        current = balances.get(ctx.author.id, _WALLET_FLOOR)
        if bet > current:
            await ctx.reply(f"가진 먹물코인({current:,})보다 많이 베팅할 수 없어요.")
            return

        balances[ctx.author.id] = current - bet
        try:
            await _replace_wallet_message(bot, channel, ctx.guild.id, balances)
        except discord.Forbidden:
            await ctx.reply(f"{channel.mention}에 메시지를 쓸 권한이 없어요. 봇 권한을 확인해주세요.")
            return

    first_card = _hilo_draw_card()
    view = HiLoView(ctx, bet, first_card)
    embed = discord.Embed(
        title="🐙 먹물 하이로우", description="다음 카드가 더 높을지 낮을지 맞춰보세요! (같은 숫자면 10:1)",
        color=discord.Color.purple(),
    )
    embed.add_field(name="첫 카드", value=_hilo_card_str(first_card), inline=True)
    embed.add_field(name="베팅액", value=f"{bet:,} 먹물코인", inline=False)
    view.message = await ctx.reply(embed=embed, view=view)


@hilo.error
async def hilo_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
        await ctx.reply("베팅액을 숫자로 적어주세요. 예: `!하이로우 100000`")
    else:
        print(f"[!하이로우 에러] {error!r}")
        await ctx.reply("처리 중 문제가 생겼어요. 다시 시도해주세요.")


@bot.tree.command(name="내전결과채널설정", description="[관리자] 팀 결과와 결과입력 버튼을 올릴 채널을 지정합니다.")
@app_commands.describe(채널="팀 결과를 올릴 채널")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_result_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    result_channels[interaction.guild_id] = 채널.id
    await interaction.response.send_message(
        f"이제부터 /내전팀 결과를 {채널.mention}에도 올리고, 거기서 결과 입력 버튼을 쓸 수 있어요.", ephemeral=True
    )


@bot.tree.command(name="내전전적채널설정", description="[관리자] 누적 승/패 전적을 기록할 채널을 지정합니다.")
@app_commands.describe(채널="누적 전적을 기록할 채널")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_record_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    record_channels[interaction.guild_id] = 채널.id
    record_message_ids.pop(interaction.guild_id, None)
    await interaction.response.send_message(f"이제부터 누적 전적을 {채널.mention}에 기록할게요.", ephemeral=True)


async def _get_record_channel(interaction: discord.Interaction):
    record_channel_id = record_channels.get(interaction.guild_id)
    if record_channel_id is None:
        return None
    channel = interaction.client.get_channel(record_channel_id)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(record_channel_id)
        except (discord.NotFound, discord.Forbidden):
            return None
    return channel


@bot.tree.command(name="전적검색", description="특정 롤 닉네임의 내전 누적 전적을 조회합니다.")
@app_commands.describe(닉네임="조회할 롤 닉네임")
async def search_record(interaction: discord.Interaction, 닉네임: str):
    channel = await _get_record_channel(interaction)
    if channel is None:
        await interaction.response.send_message(
            "아직 전적 채널이 설정되지 않았거나 찾을 수 없어요. 관리자에게 /내전전적채널설정을 요청해주세요.",
            ephemeral=True,
        )
        return

    existing_msg = await _find_record_message(interaction.client, channel, interaction.guild_id)
    records = _parse_record_text(existing_msg.content) if existing_msg else {}

    nickname_key = 닉네임.strip().lower()
    found = next(((name, rec) for name, rec in records.items() if name.strip().lower() == nickname_key), None)

    if found is None:
        await interaction.response.send_message(f"'{닉네임}'의 전적 기록을 찾을 수 없어요.", ephemeral=True)
        return

    name, (win, loss) = found
    total = win + loss
    winrate = f"{win / total * 100:.1f}%" if total > 0 else "-"
    await interaction.response.send_message(f"**{name}**: {win}승 {loss}패 (승률 {winrate})")


@bot.tree.command(name="전적수정", description="[관리자] 특정 롤 닉네임의 전적을 직접 수정합니다.")
@app_commands.describe(닉네임="수정할 롤 닉네임", 승="새로 설정할 승수", 패="새로 설정할 패수")
@app_commands.checks.has_permissions(manage_guild=True)
async def edit_record(interaction: discord.Interaction, 닉네임: str, 승: int, 패: int):
    if 승 < 0 or 패 < 0:
        await interaction.response.send_message("승/패는 0 이상이어야 해요.", ephemeral=True)
        return

    channel = await _get_record_channel(interaction)
    if channel is None:
        await interaction.response.send_message(
            "먼저 /내전전적채널설정으로 전적을 기록할 채널을 지정해주세요.", ephemeral=True
        )
        return

    existing_msg = await _find_record_message(interaction.client, channel, interaction.guild_id)
    records = _parse_record_text(existing_msg.content) if existing_msg else {}

    nickname_key = 닉네임.strip().lower()
    existing_key = next((k for k in records if k.strip().lower() == nickname_key), 닉네임)
    records[existing_key] = (승, 패)

    try:
        await _replace_record_message(interaction.client, channel, interaction.guild_id, records)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"{channel.mention}에서 메시지를 읽거나 쓸 권한이 없어요. 봇 권한을 확인해주세요.", ephemeral=True
        )
        return

    await interaction.response.send_message(f"'{existing_key}' 전적을 {승}승 {패}패로 수정했어요.", ephemeral=True)


@bot.tree.command(name="전적삭제", description="[관리자] 특정 롤 닉네임의 전적 기록을 삭제합니다.")
@app_commands.describe(닉네임="삭제할 롤 닉네임")
@app_commands.checks.has_permissions(manage_guild=True)
async def delete_record(interaction: discord.Interaction, 닉네임: str):
    channel = await _get_record_channel(interaction)
    if channel is None:
        await interaction.response.send_message(
            "먼저 /내전전적채널설정으로 전적을 기록할 채널을 지정해주세요.", ephemeral=True
        )
        return

    existing_msg = await _find_record_message(interaction.client, channel, interaction.guild_id)
    records = _parse_record_text(existing_msg.content) if existing_msg else {}

    nickname_key = 닉네임.strip().lower()
    existing_key = next((k for k in records if k.strip().lower() == nickname_key), None)

    if existing_key is None:
        await interaction.response.send_message(f"'{닉네임}'의 전적 기록을 찾을 수 없어요.", ephemeral=True)
        return

    del records[existing_key]

    try:
        await _replace_record_message(interaction.client, channel, interaction.guild_id, records)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"{channel.mention}에서 메시지를 읽거나 쓸 권한이 없어요. 봇 권한을 확인해주세요.", ephemeral=True
        )
        return

    await interaction.response.send_message(f"'{existing_key}' 전적 기록을 삭제했어요.", ephemeral=True)


@bot.tree.command(name="내전라인고정", description="[관리자] 이 채널의 '채우기' 모집에서 특정 닉네임을 특정 라인으로 고정합니다(비공개).")
@app_commands.describe(닉네임="고정할 롤 닉네임 (이미 신청한 사람)", 라인="고정할 라인")
@app_commands.choices(라인=[app_commands.Choice(name=l, value=l) for l in LINES])
@app_commands.checks.has_permissions(manage_guild=True)
async def pin_line(
    interaction: discord.Interaction,
    닉네임: str,
    라인: app_commands.Choice[str],
):
    channel_id = interaction.channel_id
    session = recruitment_sessions.get(channel_id)
    if session is None or session.get("mode") != "fill":
        await interaction.response.send_message("이 채널엔 진행 중인 '채우기' 모집이 없어요.", ephemeral=True)
        return

    line_value = 라인.value
    pinned = session.setdefault("pinned", {})
    nickname_key = 닉네임.strip().lower()

    other_pins_on_line = sum(1 for nk, l in pinned.items() if l == line_value and nk != nickname_key)
    if other_pins_on_line >= 2:
        await interaction.response.send_message(f"{line_value} 라인에는 이미 2명이 고정되어 있어요.", ephemeral=True)
        return

    pinned[nickname_key] = line_value
    await interaction.response.send_message(
        f"'{닉네임}'을(를) {line_value} 라인으로 고정했어요."
        f"\n(신청 시 주라인 또는 부라인에 {line_value}을 적었을 때만 적용되고, 다른 유저에게는 보이지 않아요.)",
        ephemeral=True,
    )
    await _send_log(
        interaction,
        f"🔒 **[라인고정]** {interaction.user.mention} → {interaction.channel.mention}에서 닉네임 `{닉네임}` 를 **{line_value}**로 고정",
    )


bot.tree.add_command(recruit_group)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
    bot.run(TOKEN)
