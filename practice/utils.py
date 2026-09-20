import re
import time
import os
import glob
import base64
from io import BytesIO

import requests
import pandas as pd
from bs4 import BeautifulSoup
from PIL import Image
from IPython.display import HTML
from collections import defaultdict

BASE = "https://www.nfm.go.kr"

# 1. 스크래핑 관련 함수
def get_csrf_token(session):
    resp = session.get(f"{BASE}/user/data/home/101/DataRelicCategoryList.do")
    soup = BeautifulSoup(resp.text, "html.parser")
    token_input = soup.select_one('input[name="CSRFToken"]')
    if token_input is None:
        raise RuntimeError("CSRF 토큰을 찾지 못했습니다.")
    return token_input.get("value")

# 2. 페이지 찾는 함수
def search_relic_list(session, csrf_token, keyword, page_size=50, delay=0.5, max_pages=200):
    SEARCH_LIST_URL = f"{BASE}/user/data/home/101/DataRelicCategoryList.do"
    results = []
    page_no = 1
    while page_no <= max_pages:
        payload = {
            "query": keyword, "pageNo": str(page_no), "pageRow": str(page_size),
            "CSRFToken": csrf_token, "collection": "coll_search", "searchField": "ALL",
        }
        resp = session.post(SEARCH_LIST_URL, data=payload)
        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.select('a[href*="DataRelicView.do"]')
        if not links:
            break
        for a in links:
            m = re.search(r"seq=([A-Za-z0-9]+)", a.get("href", ""))
            if not m:
                continue
            results.append({"seq": m.group(1), "list_title": a.get_text(strip=True), "searched_keyword": keyword})
        page_no += 1
        time.sleep(delay)
    return results

# 3. 세부 사항 스크래핑하는 함수
def get_relic_detail(session, seq, delay=0.5):
    DETAIL_URL = f"{BASE}/user/data/home/101/DataRelicView.do"
    url = f"{DETAIL_URL}?seq={seq}"
    resp = session.get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    data = {"seq": seq, "detail_url": url}
    for block in soup.select(".d-relic__data"):
        key_el, val_el = block.select_one(".d-relic__key"), block.select_one(".d-relic__value")
        if key_el is None or val_el is None:
            continue
        key_text = key_el.get_text(strip=True)
        value_text = re.sub(r"\s+", " ", val_el.get_text(separator=" ", strip=True))

        # "소장품 번호"는 사이트에서 <span>석남</span>"000001" 처럼 두 조각으로 나뉘어 있어서
        # get_text(separator=" ")가 둘 사이에 공백을 끼워넣음 -> "석남 000001"이 되는 걸 다시 붙여줌
        if key_text == "소장품 번호":
            value_text = value_text.replace(" ", "")

        data[key_text] = value_text

    # 이미지가 한 장이 아닌 유물도 있어서(예: 화첩처럼 여러 장), 중복 제거하면서 전부 모음
    seen_ids = set()
    image_urls = []
    for img in soup.select('img[src*="/common/apithumb/relic/"]'):
        m = re.search(r"apithumb/relic/(\d+)\.do", img.get("src", ""))
        if not m or m.group(1) in seen_ids:
            continue
        seen_ids.add(m.group(1))
        image_urls.append(f"{BASE}/common/apiimage/relic/{m.group(1)}.do")

    data["image_url"] = image_urls[0] if image_urls else None              # 대표(첫 번째) 이미지
    data["image_urls"] = "; ".join(image_urls) if image_urls else None     # 전체 이미지 (여러 장이면 세미콜론으로 구분)

    time.sleep(delay)
    return data

# 4. 한글과 한자 분리하는 함수
def split_hangeul_hanja(name):
    m = re.match(r'^(.*?)\(([^)]*)\)(.*)$', str(name).strip())
    if not m:
        return str(name).strip(), ""
    prefix, hanja, suffix = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    hangeul = (prefix + suffix).strip()
    return hangeul, hanja

# 5. 엑셀로 바꿀 때 열 이름 나열
def to_team_format(df, category_name, category_code, collector="전윤우"):
    result = pd.DataFrame()
    result["임시ID"] = [f"YNFM{category_code}{i+1:02d}" for i in range(len(df))]
    result["분류"] = category_name
    result["소장처"] = "국립민속박물관"
    result["소장처유물번호"] = df["소장품 번호"].astype(str)
    names = df["소장품 명칭"].apply(split_hangeul_hanja)
    result["한글명"] = [n[0] for n in names]
    result["한자명"] = [n[1] for n in names]
    result["영어명"] = ""
    result["URL"] = df["detail_url"].values
    result["수집자"] = collector

    # 핵심 14개 컬럼에 필요한 나머지 항목만 추가 (그 외 메타데이터는 버림)
    extra_cols = ["국적/시대", "용도/기능", "크기", "내용", "seq"]
    for col in extra_cols:
        if col in df.columns:
            result[col] = df[col].values

    priority_cols = ["임시ID", "분류", "소장처", "소장처유물번호", "한글명", "한자명", "영어명", "URL", "수집자",
                      "국적/시대", "용도/기능", "크기", "내용", "seq"]
    result = result[[c for c in priority_cols if c in result.columns]]

    return result

# 6. 이미지 파일명 변경 (한 유물에 사진이 여러 장이면 seq-1.jpg, seq-2.jpg ... 형태로 저장돼있음)
def rename_images_by_team_format(team_df, image_root="../image"):
    folder_map = {"흉배": "hyungbae", "단령": "dallyeong", "초상": "portrait"}
    renamed, skipped = 0, 0
    for _, row in team_df.iterrows():
        folder = folder_map.get(row["분류"])
        if folder is None:
            continue

        # seq.jpg 뿐 아니라 seq-1.jpg, seq-2.jpg 처럼 뒤에 번호 붙은 것까지 전부 찾음
        matches = sorted(glob.glob(os.path.join(image_root, folder, f"{row['seq']}*.jpg")))
        if not matches:
            skipped += 1
            continue

        for old_path in matches:
            suffix = os.path.basename(old_path)[len(str(row["seq"])):-4]  # "" 또는 "-1", "-2" ...
            new_path = os.path.join(image_root, folder, f"{row['임시ID']}{suffix}.jpg")
            os.replace(old_path, new_path)
            renamed += 1
    print(f"완료! 이름 바꾼 파일: {renamed}개, 건너뛴 파일: {skipped}개")


# 7. 이름 변경 함수 (삭제 후)
def renumber_images(image_root="../image"):
    """
    수작업으로 사진을 지운 뒤, 남은 임시ID 파일들의 -1, -2 ... 번호를 다시 순서대로 채번.
    파일이 1장만 남으면 번호(suffix)를 없애고, 2장 이상이면 -1, -2 ...로 다시 매김.
    """
    pattern = re.compile(r'^(Y[A-Za-z0-9]+?)(?:-(\d+))?\.jpg$', re.IGNORECASE)

    for folder in ["hyungbae", "dallyeong", "portrait"]:
        folder_path = os.path.join(image_root, folder)
        if not os.path.isdir(folder_path):
            continue

        groups = defaultdict(list)
        for fname in os.listdir(folder_path):
            m = pattern.match(fname)
            if not m:
                continue
            groups[m.group(1)].append(fname)

        for base_id, files in groups.items():
            files.sort()

            # 1) 이름 충돌 방지를 위해 먼저 임시 이름으로 바꿔둠
            temp_paths = []
            for fname in files:
                old_path = os.path.join(folder_path, fname)
                temp_path = os.path.join(folder_path, f"__tmp__{fname}")
                os.replace(old_path, temp_path)
                temp_paths.append(temp_path)

            # 2) 정리된 이름으로 최종 부여
            for idx, temp_path in enumerate(temp_paths):
                suffix = "" if len(temp_paths) == 1 else f"-{idx + 1}"
                new_path = os.path.join(folder_path, f"{base_id}{suffix}.jpg")
                os.replace(temp_path, new_path)

    print("번호 재정렬 완료")


# 8. 이름 바뀐 이미지 눈으로 확인하는 갤러리 (04번 수작업 확인용)
def build_review_gallery(team_df, image_root="../image", thumb_width=180):
    folder_map = {"흉배": "hyungbae", "단령": "dallyeong", "초상": "portrait"}
    cards = []
    for _, row in team_df.iterrows():
        folder = folder_map.get(row["분류"])
        if folder is None:
            continue

        # 임시ID.jpg 뿐 아니라 임시ID-1.jpg, 임시ID-2.jpg 처럼 여러 장인 것도 다 찾음
        matches = sorted(glob.glob(os.path.join(image_root, folder, f"{row['임시ID']}*.jpg")))
        if not matches:
            cards.append(f"<div style='display:inline-block;width:{thumb_width}px;margin:6px;text-align:center'>"
                          f"{row['임시ID']}<br><span style='color:red'>(이미지 없음)</span></div>")
            continue

        for path in matches:
            img = Image.open(path)
            img.thumbnail((thumb_width, thumb_width * 2))
            buf = BytesIO()
            img.save(buf, format="JPEG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            label = os.path.splitext(os.path.basename(path))[0]
            cards.append(f"<div style='display:inline-block;width:{thumb_width}px;margin:6px;text-align:center'>"
                          f"<img src='data:image/jpeg;base64,{b64}' width='{thumb_width}'><br>{label}</div>")

    return HTML("<div>" + "".join(cards) + "</div>")

# 9. 이름 바꾸고 나서 엑셀파일 임시명 변경시켜주는 함수
def sync_image_urls_with_files(team_df, image_root="../image"):
    folder_map = {"흉배": "hyungbae", "단령": "dallyeong", "초상": "portrait"}
    team_df = team_df.copy()

    new_image_url, new_image_urls = [], []

    for _, row in team_df.iterrows():
        folder = folder_map.get(row["분류"])
        temp_id = row["임시ID"]
        urls = str(row.get("image_urls", "")).split("; ") if pd.notna(row.get("image_urls")) else []

        if folder is None or not urls:
            new_image_url.append(row.get("image_url"))
            new_image_urls.append(row.get("image_urls"))
            continue

        matches = sorted(glob.glob(os.path.join(image_root, folder, f"{temp_id}*.jpg")))
        kept_indices = []
        for path in matches:
            fname = os.path.basename(path)
            m = re.match(rf'^{re.escape(temp_id)}(?:-(\d+))?\.jpg$', fname)
            if not m:
                continue
            idx = int(m.group(1)) if m.group(1) else 1  # 번호 없으면 원래 1번째 사진
            kept_indices.append(idx)

        kept_urls = [urls[i - 1] for i in sorted(kept_indices) if 0 < i <= len(urls)]
        new_image_url.append(kept_urls[0] if kept_urls else None)
        new_image_urls.append("; ".join(kept_urls) if kept_urls else None)

    team_df["image_url"] = new_image_url
    team_df["image_urls"] = new_image_urls
    return team_df