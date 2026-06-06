import urllib.request
from bs4 import BeautifulSoup

url = 'https://github.com/trending'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
try:
    html = urllib.request.urlopen(req).read()
    soup = BeautifulSoup(html, 'html.parser')
    
    repos = soup.find_all('article', class_='Box-row')
    print(f'Found {len(repos)} trending repositories.')
    for i, repo in enumerate(repos[:10], 1):
        name_tag = repo.h2.a
        link = name_tag.get('href') if name_tag else 'N/A'
        desc = repo.find('p', class_='col-9').get_text(strip=True) if repo.find('p', class_='col-9') else 'No description'
        lang = repo.find('span', itemprop='programmingLanguage').get_text() if repo.find('span', itemprop='programmingLanguage') else 'N/A'
        
        print(f'{i}. {link.strip()}')
        print(f'   Desc: {desc}')
        print(f'   Lang: {lang}')
        print('-' * 30)
except Exception as e:
    print(f'Error: {e}')
