# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Bùi Đức Vinh  **MSSV:** 2A202602801  **Ngày:** 2026-10-05

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu phải khớp với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp riêng ở `report/ONTOLOGY.md`.
>
> Graph dùng **ontology tự thiết kế** (xem `ONTOLOGY.md`). Bản đối chứng với ontology gợi ý (`KG_ONTOLOGY=hint`) nằm ở `ket_qua_benchmark_kg.hint.txt`.

## 1. Chi phí (10 điểm)

Dán 2 bảng `Indexing` và `Querying` từ `ket_qua_benchmark_kg.txt`:

```
Chat model: openai:gpt-4o-mini | Embedding: openai:text-embedding-3-small | top_k=3 | chunk_size=800 | chunks=176 | KG: 195 nodes / 436 rels

== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176     56072        0   0.00112     36.2
graph       196     96938     4708   0.01008    107.1

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.43   1.00      694       48   0.00013     1.75
graph       1.00   1.83     1997       85   0.00034     1.99
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0.00112 | 0.01008 | ×9.0 |
| Indexing giây | 36.2 | 107.1 | ×2.96 |
| Mỗi câu: USD | 0.00013 | 0.00034 | ×2.6 |
| Mỗi câu: giây | 1.75 | 1.99 | ×1.14 |
| Mỗi câu: in_tok | 694 | 1997 | ×2.88 |

**Chi phí tăng thêm đến từ đâu?**
> - **Indexing.** Graph gọi chat LLM để trích xuất JSON (vụ, người, chất, tội, giai đoạn) từ 20 bài báo. Bài dài nên có 96 938 token vào so với 56 072 token chỉ để embed. Giá token chat của gpt-4o-mini cao gấp khoảng 7,5 lần giá embedding, và có thêm 4 708 token ra (giá ×4 so với vào). Vì vậy USD tăng ×9, còn thời gian tăng ×3 do các lời gọi chat chạy tuần tự.
> - **Querying.** Mỗi câu vẫn chỉ có 1 lần gọi LLM. Phần tăng thêm là in_tok (×2.9), vì ngoài 3 chunk còn gửi kèm dữ kiện graph (vụ, người, tội, khoản luật). Truy vấn Cypher trên Neo4j gần như không tốn thời gian, nên độ trễ chỉ tăng ×1.14.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa | Định nghĩa nằm gọn trong một chunk của Điều 2 Luật PCMT, nên vector top-3 là đủ; graph chỉ thêm trích dẫn "Điều 2, khoản 4". |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa | Tên hai bị cáo tử hình nằm trong cùng đoạn tin được retrieve; `INVOLVED_IN.sentence='tử hình'` trên graph chỉ xác nhận lại. |
| Q3 | cross-kb | 0.00 / 0 | 1.00 / 2 | Graph | Flat chỉ lấy được chunk tin tức nên trả lời "Không đủ thông tin"; graph đi `Person → Case → Crime ← Điều 251 → khoản 1` để lấy "36 tháng" và "02 năm đến 07 năm". |
| Q4 | cross-kb | 0.00 / 0 | 1.00 / 2 | Graph | Flat không có chunk Điều 255; graph tìm được Hoàng Nato qua `aliases`, và nhờ `severity` lấy được khoản 4 "20 năm hoặc tù chung thân". |
| Q5 | cross-kb-multi-hop | 0.60 / 1 | 1.00 / 2 | Graph | Flat đoán ra "khoản b)" (nhầm điểm với khoản); graph so `INVOLVES.amount_g=9600` với `THRESHOLD.min_g=100` nên ra đúng "Điều 250 khoản 4, điểm b". |
| Q6 | aggregation | 0.00 / 1 | 1.00 / 1 | Graph | Top-3 chunk của Flat chỉ phủ 3 bài (bỏ sót Cái Quang Huy, Viện Pháp y); graph gom mọi `Case -[:INVOLVES]-> MDMA` (5 vụ, đúng theo dữ liệu), nhưng judge chỉ cho 1 vì gold chỉ có 3 vụ và lỗi gán khối lượng không ổn định (E5). |

## 3. Phân tích lỗi (20 điểm)

### Lỗi E2: Thiếu ngữ cảnh luật — sai khung tối đa dù graph có đủ Điều luật

- **Hiện tượng:** Với ontology gợi ý, Q4 trả lời sai mức phạt tối đa. Graph có đủ Điều 255 và cả 5 khoản, nhưng LLM nói "tối đa 7 năm", đó là khung **cơ bản** (khoản 1). Đáp án đúng là "20 năm hoặc tù chung thân" ở khoản 4.
- **Bằng chứng:**

Câu trả lời trong `ket_qua_benchmark_kg.hint.txt`:

```
--- Q4 [cross-kb] graph recall=0.67 judge=1 1.65s
Giang hồ 'Hoàng Nato' bị bắt về hành vi tổ chức sử dụng trái phép chất ma túy. Hành vi này có thể bị phạt tù tối đa 7 năm theo Điều 255 Bộ luật Hình sự.
```

Mô phỏng quy tắc lọc khoản của Bước 5 trên graph gợi ý (cổng 7688):

```cypher
MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article {id:'Điều 255 BLHS'})-[:HAS_CLAUSE]->(c:Clause)
WHERE k.name CONTAINS 'Hoàng Nato'
OPTIONAL MATCH (k)-[:INVOLVES]->(s)<-[:MENTIONS]-(c)
RETURN DISTINCT k.name, c.number, left(c.text,90) AS text, count(s)>0 OR c.number=1 AS kept_by_hint
ORDER BY k.name, c.number
```

```
Vụ bắt giữ TikToker Phannhibeauty và giang hồ 'Hoàng Nato' | 1 | 1. Người nào tổ chức sử dụng trái phép chất ma túy ... | true
Vụ bắt giữ TikToker Phannhibeauty và giang hồ 'Hoàng Nato' | 2 | 2. ... phạt tù từ 07 năm đến 15 năm                        | false
Vụ bắt giữ TikToker Phannhibeauty và giang hồ 'Hoàng Nato' | 3 | 3. ... phạt tù từ 15 năm đến 20 năm                        | false
Vụ bắt giữ TikToker Phannhibeauty và giang hồ 'Hoàng Nato' | 4 | 4. ... phạt tù 20 năm hoặc tù chung thân                   | false
Vụ bắt giữ TikToker Phannhibeauty và giang hồ 'Hoàng Nato' | 5 | 5. Người phạm tội còn có thể bị phạt tiền ...              | false
(lặp lại y hệt cho 2 vụ Hoàng Nato còn lại)
```

- **Nguyên nhân:** Lỗi nằm ở **thiết kế ontology và quy tắc Cypher của KG-3**.
  - Quy tắc gợi ý giữ "khoản 1 + khoản `MENTIONS` một chất mà vụ `INVOLVES`".
  - Điều 255 (tổ chức sử dụng) không định lượng theo chất. Khoản 2–4 tăng nặng theo số người, tái phạm, gây chết người…, nên không khoản nào `MENTIONS` etomidate hay ketamine.
  - Vì vậy chỉ khoản 1 còn lại, và LLM trả lời đúng theo ngữ cảnh nó nhận được: "tối đa 7 năm".
  - Ontology gợi ý không có thuộc tính nào để biết khoản nào là **khung cao nhất**.
- **Đề xuất sửa (đã làm):**
  - Thêm `Clause.severity` (năm tù tối đa; chung thân = 30, tử hình = 40), tính bằng regex trong `penalty_severity` (`src/graph.py`).
  - `context` luôn kèm khoản có `max(severity)` của mỗi điều luật nối tới vụ, với nhãn "khung cao nhất".
  - Kết quả với ontology của tôi:

```cypher
MATCH (a:Article {id:'Điều 255 BLHS'})-[:HAS_CLAUSE]->(c) RETURN c.number, c.severity, c.penalty ORDER BY c.number
```

```
1 | 7  | phạt tù từ 02 năm đến 07 năm
2 | 15 | phạt tù từ 07 năm đến 15 năm
3 | 20 | phạt tù từ 15 năm đến 20 năm
4 | 30 | phạt tù 20 năm hoặc tù chung thân
5 | 0  | phạt tiền từ 50.000.000 đồng đến 500.000.000 đồng, ...
```

  - Q4 lên **1.00 / 2**: "phạt tù tối đa 20 năm hoặc tù chung thân theo Điều 255 BLHS, khoản 4".
  - Đánh đổi: mỗi điều luật thêm khoảng 1 dòng dữ kiện, tức khoảng 50–80 token mỗi câu. Thang điểm cho chung thân / tử hình là quy ước. Khoản 5 (hình phạt bổ sung) có `severity=0` nên không bao giờ bị chọn làm khung cao nhất.

### Lỗi E1: Cầu nối gãy — vụ án không nối được sang luật

- **Hiện tượng:** 3 / 15 `Case` không có `CHARGED_WITH`, nên không đi được sang Điều luật.
- **Bằng chứng:**

```cypher
MATCH (k:Case) WHERE NOT (k)-[:CHARGED_WITH]->() RETURN k.name, k.doc_id, k.stage
```

```
Vụ bắt giữ Nguyễn Minh Đức                | news-100260918220613301 | bắt giữ
Vụ án tại Viện Pháp y tâm thần Trung ương | news-100260924105118645 | xét xử sơ thẩm
Vụ tông cảnh sát giao thông ở An Giang    | news-100260926112415229 | khởi tố
```

Đối chiếu bài gốc:
  - **An Giang:** tội chống người thi hành công vụ, không thuộc Chương XX BLHS. Không nối là **hợp lý**, vì KB luật không có tội này.
  - **Nguyễn Minh Đức:** bài chỉ nói bị bắt, chưa nêu tội danh. Ở giai đoạn `bắt giữ` thì không nối cũng chấp nhận được.
  - **Viện Pháp y tâm thần:** **nên nối**.
    - Bài viết dòng 78 có "thu giữ MDMA, ketamine, methamphetamine, cần sa và các dụng cụ sử dụng ma túy".
    - Bài cũng nói nhiều bị cáo là tội phạm ma túy và nhân viên y tế "cùng người bệnh sử dụng ma túy". Đó là hành vi tàng trữ / tổ chức sử dụng trái phép chất ma túy.
    - Tuy vậy tội danh chính được truy tố là đưa / nhận hối lộ.
    - LLM chỉ trả về tội chính (hối lộ). `link_entity` không tìm thấy tội đó trong 13 `Crime` của KB luật, nên `charges` rỗng.
  - Graph gợi ý chỉ có vụ An Giang bị gãy. Điều này cho thấy lỗi phụ thuộc lần chạy LLM, không phải do khác ontology.
- **Nguyên nhân:**
  - **Prompt trích xuất:** chỉ hỏi "tội danh của vụ", nên LLM chọn tội chính và bỏ qua tội ma túy phụ.
  - **Danh mục tội đóng:** tội ngoài KB luật bị loại khỏi `charges`. Điều này đúng thiết kế, vì tránh `Crime` mồ côi, nhưng cũng làm mất dấu.
- **Đề xuất sửa:**
  - Đổi prompt thành "liệt kê **mọi** tội danh có trong DANH SÁCH TỘI DANH mà bài báo nêu hoặc mô tả hành vi tương ứng, kể cả tội phụ".
  - Lưu tội ngoài danh mục vào `Case.other_charges` để không mất thông tin.
  - Khi `charges` rỗng mà vụ có `INVOLVES` chất ma túy, có thể suy luận mềm sang "tàng trữ trái phép chất ma túy" (Điều 249), gắn cờ `inferred=true` trên cạnh.
  - Đánh đổi: prompt dài hơn khoảng 30 token mỗi bài. Suy luận mềm có thể nối sai, nên cần ghi rõ trong dữ kiện gửi LLM.

### Lỗi E5: LLM lệch với graph — câu tổng hợp Q6

- **Hiện tượng:**
  - Q6 graph: recall 1.00 (đủ Cái Quang Huy, Lê Minh Thành, Viện Pháp y), nhưng judge chỉ cho 1.
  - Câu trả lời liệt kê 5 vụ, khớp đúng với Cypher bên dưới. Gold chỉ có 3 vụ.
  - Ở một lần chạy `--judge` trước (cùng code, cùng graph schema), LLM viết vụ Hoàng Nato "Có khoảng 100g MDMA bị thu giữ". Đây là lỗi **bịa số liệu**: 100g là "ma túy tổng hợp các loại", không phải MDMA.
  - Lần chạy chính thức viết đúng hơn ("Liên quan đến MDMA và ma túy tổng hợp"). Như vậy lỗi **không ổn định** giữa các lần chạy.
- **Bằng chứng:** Cypher trả lời thẳng câu hỏi:

```cypher
MATCH (k:Case)-[i:INVOLVES]->(s:Substance {name:'MDMA'})
RETURN k.name, i.amount, [(p:Person)-[:INVOLVED_IN]->(k) | p.name] AS people
```

```
Vụ tổ chức sử dụng ma túy tại Sầm Sơn                | 0,686g    | Trần Quốc An, Cao Thị Bích Hằng, Ngô Việt Dũng, Nguyễn Thị Mai Anh, Lê Văn Đông
Vụ án tại Viện Pháp y tâm thần Trung ương            |           | Trần Văn Trường, Lê Văn Đông, Bùi Thị Thanh Thủy, ...
Vụ bắt giang hồ 'Hoàng Nato' và 126 người liên quan  |           | Phạm Phương Anh, ..., Dương Minh Tuấn, ...
Vụ góp tiền mua ma túy tại Hà Nội                    | 5 viên    | Kim Xuân Tuấn, Lê Minh Thành, Trịnh Vũ Kiên, Nguyễn Quang Hưng
Vụ vận chuyển ma túy từ Đức về Việt Nam              | hơn 9,6kg | Cái Quang Huy, Nguyễn Tiến Đạt
```

  Các chất của vụ Hoàng Nato trên graph:

```cypher
MATCH (k:Case {id:'news-100260920221957595#0'})-[i:INVOLVES]->(s) RETURN collect(s.name + ' ' + i.amount)
```

```
['MDMA ', 'ma túy tổng hợp khoảng 100g ma túy tổng hợp các loại', 'etomidate hơn 1.000 đầu pod chill chứa ma túy etomidate', 'Ketamine ']
```

  - Graph **đúng**: MDMA (thuốc lắc, dòng 30 bài gốc) không có khối lượng; 100g thuộc "ma túy tổng hợp" (dòng 34).
  - `context` nối các chất vào **một dòng** dữ kiện: "Chất: MDMA, ma túy tổng hợp khoảng 100g …, etomidate …, Ketamine." LLM đã ghép "100g" sang MDMA.
  - So với gold, graph thừa 2 vụ: Sầm Sơn và Hoàng Nato. Cả hai đều thật sự có MDMA, nên đây là gold thiếu, không phải graph sai.
  - Đối chiếu Flat (judge 1, recall 0): nó liệt kê "Vụ việc của **Đức**". Thật ra đó là vụ Cái Quang Huy vận chuyển MDMA **từ Đức** (`news-100260917203001265`), tức Flat hiểu nhầm tên nước thành tên người. Flat cũng bỏ sót Viện Pháp y.
- **Nguyên nhân:**
  - **Cypher / định dạng dữ kiện trong KG-3:** nhiều chất và số lượng nằm chung một chuỗi phẳng, nên LLM dễ gán nhầm số lượng.
  - **Phép đo:** gold của Q6 chỉ có 3 vụ, nên câu trả lời đúng theo dữ liệu nhưng dài hơn bị judge coi là "thừa / không chắc chắn".
- **Đề xuất sửa:**
  - Trong `Neo4jGraph.context`, tách mỗi cạnh `INVOLVES` ra thành một dữ kiện riêng, ví dụ `Vụ X -[INVOLVES {amount:'khoảng 100g'}]-> ma túy tổng hợp`. Thêm vào `GRAPH_PROMPT` quy tắc "chỉ nêu số lượng khi dữ kiện ghi số lượng cho đúng chất đó". Token tăng nhẹ, khoảng 5 %.
  - Với câu aggregation có thể trả thẳng danh sách từ Cypher rồi để LLM chỉ diễn đạt. Cách này chính xác hơn nhưng phải phân loại câu hỏi.
  - Bổ sung vụ Sầm Sơn vào gold để phép đo công bằng.

### Lỗi E3: Trùng thực thể — một vụ ngoài đời thành nhiều `Case`

- **Hiện tượng:** Vụ bắt "Hoàng Nato" được 4 bài báo đưa tin và thành 4 node `Case`, chỉ nối nhau qua `Person`.
- **Bằng chứng:**

```cypher
MATCH (k:Case)<-[:INVOLVED_IN]-(p:Person) WHERE 'Hoàng Nato' IN p.aliases RETURN p.name, k.id, k.name, k.stage
```

```
Dương Minh Tuấn | news-100260925144412498#0 | Vụ bắt 'Hoàng Nato' và triệt phá 8 đường dây ma túy        | bắt giữ
Dương Minh Tuấn | news-100260924095400982#0 | Vụ sử dụng ma túy etomidate của Hoàng Nato và Phan Kim Nhi | bắt giữ
Dương Minh Tuấn | news-100260922111804786#0 | Vụ bắt giữ TikToker Phannhibeauty và giang hồ 'Hoàng Nato' | bắt giữ
Dương Minh Tuấn | news-100260920221957595#0 | Vụ bắt giang hồ 'Hoàng Nato' và 126 người liên quan         | bắt giữ
```

  Với `Substance` thì ontology của tôi đã hết trùng: 12 node. Graph gợi ý có 17 node, gồm `Ketamine`/`ketamine`, `Methamphetamine`/`methamphetamine`, `MDMA`/`thuốc lắc`, cùng `ma túy` và `chất ma túy`.
- **Nguyên nhân:** **Khóa định danh trong ontology.**
  - Tôi chủ động khóa `Case` theo `doc_id#i` để tránh gộp nhầm khi tên LLM đặt không ổn định.
  - Cái giá là không gộp được cùng một vụ qua nhiều bài. Ontology gợi ý khóa theo `name`, cũng không gộp được vì 4 bài đặt 4 tên khác nhau.
- **Đề xuất sửa:**
  - Thêm bước hậu xử lý sau `build_graph`: hai `Case` có cùng người chính (`role` bị can / bị cáo), cùng `Crime` và ngày cách nhau ≤ 7 ngày thì nối bằng `(:Case)-[:SAME_EVENT]->(:Case)`. Không `MERGE` cứng, để còn giữ nguồn của từng bài.
  - `context` đếm vụ theo thành phần liên thông `SAME_EVENT`.
  - Đánh đổi: thêm một truy vấn O(n²) trên số Case (nhỏ). Có rủi ro nối nhầm hai vụ khác nhau của cùng một người.

## 4. Kết luận (5 điểm)

Khi nào nên dùng KG, khi nào Flat RAG là đủ? Dẫn số liệu ở mục 1–2.

> - **Flat RAG là đủ** khi câu trả lời nằm trong một đoạn văn.
>   - Q1 (định nghĩa luật) và Q2 (tin tức một bài) đều hòa: 1.00 / 2 cho cả hai.
>   - Flat rẻ hơn ×2.6 mỗi câu, indexing rẻ hơn ×9, và không cần Neo4j.
> - **Nên dùng KG** khi câu hỏi phải **nối hai nguồn** hoặc **gom nhiều tài liệu**.
>   - Q3, Q4 (cross-kb): Flat 0.00 / 0, Graph 1.00 / 2.
>   - Q5 (multi-hop có định lượng): 0.60 / 1 so với 1.00 / 2.
>   - Q6 (aggregation): recall 0.00 so với 1.00.
>   - Tính trung bình, recall tăng 0.43 → 1.00 và judge tăng 1.00 → 1.83. Đổi lại chỉ +0.24 giây và +$0.00021 mỗi câu, cộng khoảng $0.009 indexing một lần.
> - **Ontology quyết định chất lượng KG.**
>   - Cùng pipeline, ontology gợi ý đạt recall 0.83 / judge 1.67 với 4 559 in_tok mỗi câu.
>   - Ontology của tôi (`severity`, `THRESHOLD` theo gam, chuẩn hóa chất) đạt 1.00 / 1.83 với 1 997 in_tok.
>   - KG chỉ đáng tiền khi thiết kế quanh đúng câu hỏi cần trả lời (competency questions).
>   - Phần đắt và dễ lỗi nhất vẫn là các bước dùng LLM: trích xuất (E1) và diễn đạt dữ kiện (E5). Với corpus thay đổi thường xuyên, hoặc câu hỏi chủ yếu single-hop, Flat RAG vẫn là lựa chọn hợp lý hơn.

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.04s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = openai:gpt-4o-mini | embedding = openai:text-embedding-3-small
[OK] KG-2 build_graph: 148 node / 353 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 18 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00082. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.
Người đã chọn cho `kg_my_case.png`: **Cái Quang Huy**. Truy vấn Q-D mở rộng theo ontology của tôi, thêm bước chọn khoản bằng `THRESHOLD`:

```cypher
MATCH p=(:Person {name:'Cái Quang Huy'})-[:INVOLVED_IN]->(k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)
OPTIONAL MATCH q=(k)-[:INVOLVES|LOCATED_IN]->()
OPTIONAL MATCH r=(a)-[:HAS_CLAUSE]->(:Clause)-[t:THRESHOLD]->(:Substance)<-[i:INVOLVES]-(k)
  WHERE t.min_g <= i.amount_g AND (t.max_g IS NULL OR i.amount_g < t.max_g)
RETURN p, q, r;
```

Kết quả: Cái Quang Huy → Vụ vận chuyển ma túy từ Đức về Việt Nam → tội vận chuyển trái phép chất ma túy ← Điều 250 BLHS → khoản 4 -[THRESHOLD điểm b, ≥ 100 g]→ MDMA ← INVOLVES (9 600 g). Vụ còn có Ketamine và địa điểm Hà Nội.

## Vấn đề gặp phải (không tính điểm)

- **LLM trích xuất "bẩn" ở các phiên bản prompt đầu.**
  - Biểu hiện:
    - Ghi nguyên văn chữ "chuỗi rỗng" vào trường trống.
    - Tạo người giả "Người Trung Quốc 1..7".
    - Bỏ sót MDMA khi bài gọi là ma túy "kẹo".
    - Trích teaser "tin liên quan" thành một vụ riêng.
  - Đã sửa:
    - Thêm quy tắc vào prompt.
    - Thêm `_blank_placeholders`.
    - Lọc tên chứa chữ số.
  - Riêng lỗi teaser vẫn xuất hiện ngẫu nhiên ở vài lần chạy, vì LLM không tất định dù `temperature=0`.
- **Q1 graph từng trả lời "Không đủ thông tin"** vì dữ kiện graph không liên quan làm nhiễu. Đã sửa bằng `matching_clauses`, đi từ Article sang Clause theo bigram, và chỉ dùng khi không có vụ nào được chạm tới.
- **Q6 graph từng bỏ sót vụ Viện Pháp y** vì prompt bị ngập khoảng 30 dữ kiện luật. Đã sửa: chỉ thêm luật khi câu hỏi có từ khóa pháp lý, và gom khoản theo điều.
- **Để đo ontology gợi ý mà không ghi đè graph chính,** tôi chạy thêm một container `neo4j:5` ở cổng 7475/7688 với `NEO4J_URI=bolt://localhost:7688 KG_ONTOLOGY=hint python bench_kg.py --judge`.
