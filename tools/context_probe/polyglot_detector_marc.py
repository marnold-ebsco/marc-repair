import re

class MARCPolyglotDetector:
    """
    A specialized polyglot detector optimized for cataloged metadata fields 
    (such as MARC 1XX/245/5XX). It is highly resilient against stripped, missing, 
    or corrupted diacritics/umlauts, and it filters out cataloging noise.
    """
    
    def __init__(self):
        # 1. Compile native script patterns
        self.chinese_pattern = re.compile(r"[一-鿿]")
        self.japanese_pattern = re.compile(r"[぀-ゟ゠-ヿ]")
        self.korean_pattern = re.compile(r"[가-힣㄰-㆏]")
        self.russian_native_pattern = re.compile(r"[Ѐ-ӿ]")
        
        # 2. Universal catalog subfield/punctuation noise cleaner
        # Removes subfield delimiters like $a, $b, $c, slashes, trailing periods, brackets
        self.noise_pattern = re.compile(r"\$[a-z0-9]|[a-z]|[.\[\]()/,:;=\-+•\s]+")

        # 3. Language Profiles: Built heavily using diacritic-agnostic structures
        # and classic vocabulary frequently surfacing in catalog descriptions/titles.
        self.profiles = {
            "german": {
                "words": {"der", "die", "das", "und", "ist", "von", "mit", "zu", "im", "fur", "auf", "eine", "ein", "aus", "nach"},
                "trigrams": {"sch", "ich", "cht", "ent", "ein", "der", "die", "und", "geh", "bue", "rue", "ers", "ung", "end", "lic"},
                "weight": 1.0
            },
            "french": {
                "words": {"le", "la", "les", "et", "un", "une", "des", "dans", "par", "pour", "en", "sur", "du", "au", "aux", "est"},
                "trigrams": {"les", "ent", "que", "pour", "tion", "ion", "dan", "des", "par", "aux", "eme", "ais", "oit", "ett", "eur"},
                "weight": 1.0
            },
            "portuguese": {
                "words": {"o", "a", "os", "as", "e", "do", "da", "dos", "das", "em", "um", "uma", "com", "para", "por", "na", "no", "sao"},
                "trigrams": {"cao", "oes", "com", "para", "ade", "ent", "ment", "ist", "ais", "est", "que", "uma", "dos", "das", "ame"},
                "weight": 1.1
            },
            "romanian": {
                "words": {"si", "cu", "din", "de", "la", "un", "o", "ai", "ale", "lui", "sunt", "este", "in", "pentru", "ca", "sau"},
                "trigrams": {"sunt", "lui", "lor", "ilor", "atilor", "rea", "ari", "int", "est", "din", "tru", "pen", "eaz", "ati"},
                "weight": 1.2
            },
            "polish": {
                "words": {"i", "w", "z", "na", "do", "w", "o", "po", "za", "jest", "dla", "jak", "sie", "nie", "od"},
                "trigrams": {"prz", "szy", "czy", "nie", "ich", "ego", "owie", "ska", "ski", "pod", "chw", "dzi", "iem", "ach", "wsk"},
                "weight": 1.2
            },
            "hungarian": {
                "words": {"a", "az", "es", "egy", "hogy", "nem", "volt", "mint", "meg", "van", "de", "is", "be", "ki", "el"},
                "trigrams": {"egy", "hogy", "ban", "ben", "nak", "nek", "esz", "asz", "bol", "bel", "rol", "rel", "szan", "obb", "let"},
                "weight": 1.3
            },
            "russian_romanized": {
                "words": {"i", "v", "na", "chto", "eto", "s", "k", "kak", "tako", "ego", "iz", "za", "po", "dlya", "ot"},
                "trigrams": {"ogo", "ikh", "aya", "vsh", "chsh", "shch", "zhn", "vst", "ost", "nie", "eni", "ovs", "ogo", "iyz", "ska"},
                "weight": 1.1
            },
            "korean_romanized": {
                "words": {"neun", "eun", "seo", "reul", "eul", "ga", "i", "ui", "ro", "annyeong", "gamsa", "daebak"},
                "trigrams": {"mda", "nida", "eon", "haen", "heun", "geo", "reul", "seo", "eun", "neu", "guk", "han", "mun", "hak"},
                "weight": 1.4
            },
            "chinese_pinyin": {
                "words": {"de", "shi", "yi", "zao", "zhong", "guo", "yu", "wen", "ji", "nian", "ban", "zhu", "shang"},
                "trigrams": {"zho", "guo", "wen", "hua", "jia", "sha", "yan", "xia", "xua", "lia", "ing", "ang", "ong", "shi", "jia"},
                "weight": 1.4
            },
            "japanese_romaji": {
                "words": {"no", "to", "wa", "ga", "wo", "ni", "de", "mo", "kara", "desu", "masu", "shite", "kara", "suru"},
                "trigrams": {"des", "mas", "shi", "ken", "shis", "kat", "tsu", "sho", "kyo", "mon", "gaku", "bun", "ron", "kai", "jin"},
                "weight": 1.4
            }
        }

    def clean_marc_field(self, text):
        """Filters out typical MARC structural elements and subfields to clean the text."""
        # Convert to lowercase for comparison, leaving native blocks readable by checking raw later
        clean = self.noise_pattern.sub(" ", text.strip())
        return clean.strip()

    def detect(self, raw_text):
        """Analyzes a MARC field text block and calculates language confidence rankings."""
        if not raw_text or not raw_text.strip():
            return {"language": "Unknown/Empty", "confidence": 0.0, "scores": {}}

        # 1. Native Character Scans (Extremely accurate for MARC field variants)
        total_len = len(raw_text.replace(" ", ""))
        if total_len > 0:
            if len(self.chinese_pattern.findall(raw_text)) / total_len > 0.12:
                return {"language": "Chinese (Native Script)", "confidence": 0.95, "method": "Native Unicode Scan"}
            if len(self.japanese_pattern.findall(raw_text)) / total_len > 0.12:
                return {"language": "Japanese (Native Script)", "confidence": 0.95, "method": "Native Unicode Scan"}
            if len(self.korean_pattern.findall(raw_text)) / total_len > 0.12:
                return {"language": "Korean (Native Script)", "confidence": 0.95, "method": "Native Unicode Scan"}
            if len(self.russian_native_pattern.findall(raw_text)) / total_len > 0.12:
                return {"language": "Russian (Native Cyrillic)", "confidence": 0.95, "method": "Native Unicode Scan"}

        # 2. Text Normalization for flattened Latin profiles (resilient against missing/corrupt diacritics)
        cleaned_text = self.clean_marc_field(raw_text.lower())
        words = cleaned_text.split()
        
        if not words:
            return {"language": "Unknown/Short Metadata", "confidence": 0.0, "scores": {}}

        # Extract structural trigrams from ASCII metadata
        trigrams = []
        for word in words:
            if len(word) >= 3:
                for i in range(len(word) - 2):
                    trigrams.append(word[i:i+3])

        # 3. Match Evaluation
        scores = {lang: 0.0 for lang in self.profiles}
        
        for lang, rules in self.profiles.items():
            # Check high-frequency core catalog words
            word_matches = sum(1 for w in words if w in rules["words"])
            # Check phonetic structural configurations
            trigram_matches = sum(1 for t in trigrams if t in rules["trigrams"])
            
            # Weighted calculation factoring in language characteristics
            score = ((word_matches * 3.0) + (trigram_matches * 0.8)) * rules["weight"]
            scores[lang] = round(score, 2)

        # Sort and return best match
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_lang, top_score = sorted_scores[0]
        
        # Calculate localized confidence ratio
        total_score_sum = sum(scores.values())
        confidence = round(top_score / total_score_sum, 2) if total_score_sum > 0 else 0.0

        if top_score < 1.0:
            return {"language": "English / Indeterminate", "confidence": 0.60, "scores": sorted_scores[:3]}

        # Map display names nicely
        display_names = {
            "russian_romanized": "Russian (Romanized)",
            "korean_romanized": "Korean (Romanized)",
            "chinese_pinyin": "Chinese (Pinyin)",
            "japanese_romaji": "Japanese (Romaji)"
        }
        final_lang_name = display_names.get(top_lang, top_lang.capitalize())

        return {
            "language": final_lang_name,
            "confidence": confidence if confidence > 0 else 0.5,
            "scores": sorted_scores[:3]
        }

# Practical Demonstration with typical MARC fragments
if __name__ == "__main__":
    detector = MARCPolyglotDetector()
    
    test_fields = [
        "245 10 $a Histoire de la litterature francaise / $c par Jean Durand.",  # French
        "245 10 $a Introduction to algorithm design.",                          # English
        "500 ## $a Enthalt: Bd. 1. Die Fruehzeit -- Bd. 2. Das Spatmittelalter.", # German (Umlauts removed/flattened)
        "100 1# $a Sienkiewicz, Henryk, $d 1846-1916.",                         # Polish (Name footprint)
        "245 00 $a Zhongguo li shi wen hua jie shao.",                          # Chinese Pinyin
        "245 10 $a 國語辭典 / $c 中華書局.",                                      # Chinese Native
        "504 ## $a Tartalomjegyzék és bibliográfia: p. 234-240.",                 # Hungarian (Corrupt/flat accents)
    ]
    
    print("--- MARC FIELD DETECTION RESULTS ---")
    for field in test_fields:
        res = detector.detect(field)
        print(f"\nField: \"{field}\"")
        print(f"-> Detected: {res['language']} (Confidence: {res['confidence']})")
