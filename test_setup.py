import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from graph import build_graph, create_llm
from tools import ALL_TOOLS
from memory import Memory

print("[OK] imports successful")
print(f"Tools: {len(ALL_TOOLS)} -> {[t.name for t in ALL_TOOLS]}")

mem = Memory()
print(f"Memory: OK ({len(mem.list_all().get('notes', []))} notes)")

graph = build_graph()
print(f"Graph: {type(graph).__name__}")
print("[OK] all components ready")
