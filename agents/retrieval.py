"""BM25-lite with Latin words and Chinese character bigrams, no embedding service."""
import math
import re
from collections import Counter


def tokens(text):
    result = re.findall(r"[a-z0-9_]+", text.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        result.extend(run if len(run) == 1 else (run[i:i+2] for i in range(len(run)-1)))
    return result


def rank(query, documents, limit=3):
    if not documents:
        return []
    terms = set(tokens(query))
    bags = [Counter(tokens(text)) for _, text in documents]
    average = sum(sum(b.values()) for b in bags) / len(bags) or 1
    scored = []
    for (key, _), bag in zip(documents, bags):
        score = 0
        for term in terms:
            df = sum(term in b for b in bags)
            tf = bag[term]
            idf = math.log(1 + (len(bags)-df+0.5)/(df+0.5))
            score += idf * tf * 2.2 / (tf + 1.2*(0.25+0.75*sum(bag.values())/average))
        if score > 0:
            scored.append((key, score))
    return sorted(scored, key=lambda item: (-item[1], item[0]))[:limit]
