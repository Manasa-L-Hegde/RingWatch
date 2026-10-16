import memory
from agent import investigate

print("=" * 80)
print("DIAGNOSTIC RUN: Resetting memory for analyst a9 and running order o546 3 times")
print("=" * 80)

memory.reset_analyst("a9")

for run_i in range(1, 4):
    print(f"\n--- Run {run_i} ---")
    res = investigate(analyst_id="a9", order_id="o546", use_llm=True)
    raw = res.get("raw_sentence", "")
    final = res.get("sentence", "")
    src = res.get("sentence_source", "")
    reason = res.get("sentence_rejected_reason")
    
    print(f"Outcome: {res.get('outcome')}, Verdict: {res.get('verdict')}, Sentence Source: {src}")
    print(f"Raw LLM Sentence: {raw}")
    if src == "llm":
        print("Status: ACCEPTED")
    else:
        print("Status: REJECTED")
        print(f"Rejection Reason: {reason}")
        print(f"Fallback Template Used: {final}")

print("\n" + "=" * 80)
print("DIAGNOSTIC RUN COMPLETE")
print("=" * 80)
