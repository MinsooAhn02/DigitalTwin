# How to Write the Report

## 교수님 지시사항 (원본)
- abstarct ... conclusion
- \+ contribution (내가 뭘 했는지 -> 비율 분배가 아니라 어떤 새로운 점을 넣었는지 기본에)
- what tech diff. sth special 한게 뭐있는지
- 파일 2개: w/o contribution + w/ contribution

---

## 제출 파일 구성

| 파일 | 포함 내용 |
|------|-----------|
| `report_base.pdf` | Contribution 섹션 **없는** 버전 (표준 IEEE 구조만) |
| `report_contribution.pdf` | Contribution 섹션 **포함된** 최종 버전 |

---

## IEEE 섹션 순서

### 1. Title & Authors
- 프로젝트 제목, 저자명, 소속, 날짜

### 2. Abstract (150~250 words)
- 문제 정의 → 접근 방법 → 핵심 결과 → 의의
- 수식/인용 없이 독립적으로 이해 가능하게 작성

### 3. Index Terms
- 5개 내외 키워드 (e.g., Digital Twin, Real-time Simulation, Physics-based Modeling, ...)

### 4. Introduction
- 배경 및 동기
- 문제 정의
- 논문 기여 요약 (bullet point)
- 논문 구성 안내 ("Section II describes ...")

### 5. Related Work
- 기존 연구 정리
- 우리 접근법이 어떻게 다른지 명시

### 6. System Design / Architecture
- 전체 시스템 구조 (다이어그램 포함)
- 주요 컴포넌트 설명

### 7. Implementation
- 사용 기술 스택
- 핵심 알고리즘/로직
- 필요시 코드 스니펫 포함

### 8. Evaluation / Results
- 실험 설정
- 결과 (표, 그래프)
- 정량적 수치 포함

### 9. Discussion
- 결과 해석
- 한계점
- 향후 연구 방향

### 10. Conclusion
- 기여 요약
- 실용적 의의

### 11. ★ Contribution ← w/ contribution 버전에만 포함
> 비율(%) 분배가 아닌, **기술적으로 새로운 점** 중심으로 서술

작성 구조:
1. **What was already there (base)** — 기존에 있던 것
2. **What I specifically added** — 내가 새로 추가하거나 변경한 것
3. **Why it is technically different or special** — 왜 기술적으로 다른지/특별한지
4. **Evidence** — 스크린샷, 수치, 코드 diff 등 증거

### 12. References
- IEEE 인용 형식: `[1] A. Author, "Title," *Journal*, vol. X, no. Y, pp. Z, Year.`
