"""Write a stand-in for /Users/Zachary/xfile.txt, the grader's input.

The original data file was never saved alongside main.cpp, so this one is
invented. It is shaped to what main.cpp actually parses, not to any assignment
text (there is none in the archive):

  line 1           the 30-answer key
  then 25 times    a student's name, then that student's 30 answers

and every line ends in a bare CR, the old Mac line ending. main.cpp splits
names with getline(..., '\\r') and skips lines with ignore(200, '\\r'); with
LF endings it reads garbage. The CRs are generated here rather than committed
because a stray autocrlf would silently rewrite them.

Deterministic: no RNG. Each student's answers are the key with a fixed number
of positions flipped, so the scores run 100% down to 0%. Three students get one
unreadable mark ('?'), which the program prints as x. The last student scores
zero, which main.cpp's frequency table has no `case` for.

The names are computing pioneers, chosen so that nobody mistakes this for a
real class roster.
"""

import sys

KEY = "TFFTTFTFTTFFTFTFTTFTFFTTFTFTFT"

# (name, number of wrong answers)
STUDENTS = [
    ("Ada Lovelace", 0), ("Alan Turing", 1), ("Grace Hopper", 2),
    ("Edsger Dijkstra", 3), ("Barbara Liskov", 4), ("Donald Knuth", 5),
    ("Margaret Hamilton", 7), ("Ken Thompson", 9), ("Dennis Ritchie", 10),
    ("Frances Allen", 12), ("John Backus", 13), ("Radia Perlman", 14),
    ("Niklaus Wirth", 15), ("Shafi Goldwasser", 16), ("Tony Hoare", 17),
    ("Leslie Lamport", 18), ("Bjarne Stroustrup", 19), ("Jean Sammet", 20),
    ("Fran Bilas", 21), ("Kay McNulty", 22), ("Betty Holberton", 24),
    ("Ruth Teitelbaum", 25), ("Marlyn Meltzer", 26), ("Adele Goldberg", 28),
    ("Sophie Wilson", 30),
]

FLIP = {"T": "F", "F": "T"}


def answers(index, wrong):
    # A per-student permutation of the 30 positions; flip the first `wrong`.
    order = []
    for p in [(index * 7 + k * 11) % 30 for k in range(30)] + list(range(30)):
        if p not in order:
            order.append(p)
    a = list(KEY)
    for p in order[:wrong]:
        a[p] = FLIP[a[p]]
    if index in (3, 11, 19):
        a[order[-1]] = "?"
    return "".join(a)


def main(path):
    assert len(KEY) == 30 and len(STUDENTS) == 25
    lines = [KEY]
    for i, (name, wrong) in enumerate(STUDENTS):
        lines += [name, answers(i, wrong)]
    with open(path, "w", newline="") as f:
        f.write("\r".join(lines) + "\r")


if __name__ == "__main__":
    main(sys.argv[1])
