from bs4 import BeautifulSoup

def parse_detail_html(html: str) -> dict:
    """
    Parse Eastmoney guba detail page HTML.
    Returns a dict with 'time' and 'full_text'.
    If parsing fails, values will be empty strings.
    """
    if not html:
        return {"time": "", "full_text": ""}
        
    soup = BeautifulSoup(html, features="lxml")
    
    try:
        time_str = soup.find("div", {"class": "time"}).text
    except (ValueError, AttributeError):
        time_str = ""
        
    try:
        if soup.find("div", {"id": "post_content"}):
            full_text = soup.find("div", {"id": "post_content"}).text
        else:
            newstext = soup.find("div", {"class": "newstext"})
            full_text = newstext.text if newstext else ""
    except (ValueError, AttributeError):
        full_text = ""
        
    return {"time": time_str, "full_text": full_text}
