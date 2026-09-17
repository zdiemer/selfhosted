// repl.cpp — a driver for zdiemer/my-data-structure.
//
// That repository is two header files and nothing else: no main(), no Makefile,
// no README, two commits on 2015-01-26. It was written against an instructor's
// driver that was never committed, so it has never been runnable from a clone.
// This file is the harness that makes it runnable, and it lives here in
// selfhosted rather than there because the repo is a frozen archive.
//
// Everything this REPL reports about the table's behaviour is read from the
// real object. Nothing is simulated.

// Standard headers FIRST, uncorrupted.
//
// MyDS.h includes <string>, <vector>, <list>, <iostream> and <algorithm> itself.
// Pulling them in here means their include guards make those copies no-ops, so
// the macro below can only ever reach MyDS.h and MyHash.h. Defining
// `private public` ahead of libstdc++ headers does compile, usually — but it
// changes member access on standard types, which changes standard-layout-ness,
// which can select different template specialisations. Not a trade worth making
// for a demo.
#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <list>
#include <sstream>
#include <string>
#include <vector>

// MyDS keeps nodes/max_size/size private and exposes no accessors at all — not
// even size(). The bucket distribution and the rehash ARE the assignment, and
// the repo cannot be edited, so we reach in. Undefined behaviour by the letter
// of the standard; confined to one translation unit, two headers, and undone on
// the next line.
#define private public
#include "MyDS.h"
#undef private

// MyDS.h does `using namespace std;` at global scope, so this file inherits it
// whether or not that is wanted. Noted rather than worked around — working
// around it would mean editing the header.

namespace {

const char *const kWordsDir = "/opt/myds/words";
const char *const kWarningsFile = "/opt/myds/build-warnings.txt";

// MyHash is a functor over `string` taken BY VALUE, so every call copies the
// key — including inside rehash()'s inner loop. Reproduced here rather than
// called so the REPL can show the raw value before the modulo.
unsigned long djb2(const std::string &s) {
    unsigned long h = 5381;
    for (int i = 0; i < (int)s.length(); i++)
        h = ((h << 5) + h) + s[i];
    return h;
}

struct Stats {
    long elements = 0;       // summed across every bucket: the real count
    long occupied = 0;       // buckets with at least one entry: the real count
    long longest = 0;        // longest chain
    long longestIndex = -1;
};

Stats measure(const MyDS &ds) {
    Stats st;
    for (std::size_t i = 0; i < ds.nodes.size(); i++) {
        const std::size_t n = ds.nodes[i].size();
        if (n == 0) continue;
        st.occupied++;
        st.elements += (long)n;
        if ((long)n > st.longest) {
            st.longest = (long)n;
            st.longestIndex = (long)i;
        }
    }
    return st;
}

void printBanner() {
    std::cout <<
        "\n"
        "  MyDS — a separate-chaining hash table over std::string\n"
        "  Florida State University coursework, January 2015\n"
        "  Zachary Diemer\n"
        "\n"
        "  The whole repository is two headers, MyDS.h and MyHash.h. There is no\n"
        "  main(), no Makefile and no README; this REPL was written in 2026 to\n"
        "  make it runnable, and it reads the table's private members directly so\n"
        "  that everything below is measured rather than described.\n"
        "\n"
        "  Type `help` for commands, or `demo` for the guided tour.\n"
        "\n";
}

void printHelp() {
    std::cout <<
        "\n"
        "  insert <key>     push(key) — then report the bucket and chain length\n"
        "  search <key>     search(key) — and how many nodes it had to walk\n"
        "  stats            elements, buckets, load factor, longest chain\n"
        "  chain <key>      the raw hash, the bucket index, and what is in it\n"
        "  hash <key>       the djb2 value alone, before and after the modulo\n"
        "  load common|fsu  bulk-insert a bundled word list\n"
        "  demo             the guided tour: force a rehash and watch it break\n"
        "  warnings         the compiler warnings this code still produces\n"
        "  reset            a fresh MyDS\n"
        "  help             this\n"
        "  quit             exit (Ctrl-D also works)\n"
        "\n"
        "  HONEST NOTES — this REPL can do nothing the original class could not:\n"
        "\n"
        "   * There is no remove(). MyDS has exactly four public members: the\n"
        "     constructor, the destructor, push() and search(). Deleting a key\n"
        "     was not part of the assignment and was never written.\n"
        "\n"
        "   * search() is std::find over the bucket's std::list — linear in the\n"
        "     chain, not O(1). The hash gets you to the right list; after that it\n"
        "     is a walk.\n"
        "\n"
        "   * push() does not deduplicate. Inserting \"seminole\" twice stores it\n"
        "     twice, and search() still finds it after you insert it 150 times.\n"
        "     That is also how `demo` forces a rehash.\n"
        "\n"
        "   * `stats` prints the class's own `size` field next to a real count of\n"
        "     occupied buckets. They start equal and stay equal until a rehash\n"
        "     splits two keys that used to share a bucket — at which point the\n"
        "     real count goes up and `size` does not, because rehash() rebuilds\n"
        "     the vector and never recomputes it. `demo` makes that happen.\n"
        "\n";
}

void cmdStats(const MyDS &ds) {
    const Stats st = measure(ds);
    const double load = ds.nodes.empty()
        ? 0.0 : (double)st.elements / (double)ds.nodes.size();
    const double emptyPct = ds.nodes.empty()
        ? 0.0 : 100.0 * (1.0 - (double)st.occupied / (double)ds.nodes.size());

    std::cout << std::fixed << std::setprecision(5);
    std::cout << "  elements (counted)   " << st.elements << "\n";
    std::cout << "  buckets              " << ds.nodes.size() << "\n";
    std::cout << "  max_size field       " << ds.max_size << "\n";
    std::cout << "  occupied (counted)   " << st.occupied << "\n";
    std::cout << "  size field           " << ds.size << "\n";
    std::cout << "  load factor          " << load << "\n";
    std::cout << std::setprecision(2);
    std::cout << "  empty buckets        " << emptyPct << "%\n";
    if (st.longestIndex >= 0)
        std::cout << "  longest chain        " << st.longest
                  << " (bucket " << st.longestIndex << ")\n";

    // The divergence is the point, so say so rather than leaving it to be
    // noticed. push() increments `size` only when a bucket goes from empty to
    // non-empty, and rehash() swaps in a freshly built vector without touching
    // it — so the moment a rehash splits two keys that shared a bucket, the
    // real count moves and the field does not. Nothing ever corrects it.
    if ((long)ds.size != st.occupied) {
        std::cout << "\n"
                  << "  ^ the `size` field says " << ds.size
                  << " but " << st.occupied << " buckets are actually occupied.\n"
                  << "    A rehash split keys that used to share a bucket. push()\n"
                  << "    increments `size` only on an empty -> non-empty transition,\n"
                  << "    and rehash() never recomputes it, so it is now wrong by "
                  << (st.occupied - (long)ds.size) << "\n"
                  << "    and will stay wrong for the life of the object.\n";
    }
    std::cout << "\n";
}

void cmdInsert(MyDS &ds, const std::string &key) {
    const int beforeMax = ds.max_size;
    const unsigned long raw = djb2(key);
    const std::size_t bucket = raw % (unsigned long)beforeMax;

    ds.push(key);

    if (ds.max_size != beforeMax) {
        std::cout << "  inserted \"" << key << "\" into bucket " << bucket << "\n"
                  << "  REHASH: max_size " << beforeMax << " -> " << ds.max_size
                  << " (a chain reached 150)\n"
                  << "  \"" << key << "\" now lives in bucket "
                  << (raw % (unsigned long)ds.max_size) << "\n\n";
        return;
    }
    std::cout << "  inserted \"" << key << "\" into bucket " << bucket
              << " (chain length now " << ds.nodes[bucket].size() << ")\n\n";
}

void cmdSearch(MyDS &ds, const std::string &key) {
    const std::size_t bucket = djb2(key) % (unsigned long)ds.max_size;

    // Count the walk ourselves. search() returns a bool and tells you nothing
    // about what it cost, which is exactly the thing worth seeing here.
    long walked = 0;
    for (std::list<std::string>::const_iterator it = ds.nodes[bucket].begin();
         it != ds.nodes[bucket].end(); ++it) {
        walked++;
        if (*it == key) break;
    }

    const bool found = ds.search(key);
    std::cout << "  search(\"" << key << "\") -> " << (found ? "true" : "false")
              << "\n  bucket " << bucket << ", walked " << walked << " of "
              << ds.nodes[bucket].size() << " node(s)\n";
    if (!found && ds.nodes[bucket].size() > 1) {
        std::cout << "  a miss walks the ENTIRE chain — std::find has nowhere to stop early\n";
    }
    std::cout << "\n";
}

void cmdChain(const MyDS &ds, const std::string &key) {
    const unsigned long raw = djb2(key);
    const std::size_t bucket = raw % (unsigned long)ds.max_size;
    const std::list<std::string> &l = ds.nodes[bucket];

    std::cout << "  djb2(\"" << key << "\") = " << raw << "\n"
              << "  % " << ds.max_size << " = bucket " << bucket << "\n"
              << "  chain length " << l.size() << "\n";
    int shown = 0;
    for (std::list<std::string>::const_iterator it = l.begin();
         it != l.end() && shown < 20; ++it, ++shown) {
        std::cout << "    [" << shown << "] " << *it << "\n";
    }
    if ((long)l.size() > shown)
        std::cout << "    ... +" << ((long)l.size() - shown) << " more\n";
    std::cout << "\n";
}

void cmdHash(const MyDS &ds, const std::string &key) {
    const unsigned long raw = djb2(key);
    std::cout << "  djb2(\"" << key << "\")  = " << raw << "\n"
              << "  % 60000 (initial) = " << (raw % 60000UL) << "\n"
              << "  % " << ds.max_size << " (current) = "
              << (raw % (unsigned long)ds.max_size) << "\n\n";
}

bool cmdLoad(MyDS &ds, const std::string &name) {
    // Reject anything with a path separator: this process has no business
    // opening files outside its word list directory, and a REPL that takes a
    // filename is a file-read primitive if you let it be one.
    if (name.find('/') != std::string::npos || name.find("..") != std::string::npos) {
        std::cout << "  unknown list: " << name << " (try `common` or `fsu`)\n\n";
        return false;
    }
    const std::string path = std::string(kWordsDir) + "/" + name + ".txt";
    std::ifstream in(path.c_str());
    if (!in) {
        std::cout << "  unknown list: " << name << " (try `common` or `fsu`)\n\n";
        return false;
    }

    const int beforeMax = ds.max_size;
    long n = 0;
    std::string word;
    const std::chrono::steady_clock::time_point t0 = std::chrono::steady_clock::now();
    while (std::getline(in, word)) {
        if (word.empty()) continue;
        ds.push(word);
        n++;
    }
    // Microseconds, not milliseconds: 4,488 inserts land comfortably under 1 ms
    // and "0 ms" reads like the command did nothing.
    const long us = (long)std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::steady_clock::now() - t0).count();

    std::cout << "  inserted " << n << " words in " << us << " us\n";
    if (ds.max_size != beforeMax)
        std::cout << "  max_size " << beforeMax << " -> " << ds.max_size << "\n";
    else
        std::cout << "  no rehash: " << n << " distinct-ish words across "
                  << ds.nodes.size() << " buckets never fills a chain to 150\n";
    std::cout << "\n";
    return true;
}

void cmdWarnings() {
    std::ifstream in(kWarningsFile);
    if (!in) {
        std::cout << "  (no warnings file in this image)\n\n";
        return;
    }
    std::cout << "\n  What g++ -std=c++17 -Wall -Wextra still says about these two\n"
                 "  headers, eleven years on:\n\n";
    std::string line;
    while (std::getline(in, line)) std::cout << "  " << line << "\n";
    std::cout << "\n";
}

// Find a string that lands in `bucket` under the CURRENT max_size, so that a
// lookup which is certain to miss is nonetheless certain to walk the whole
// chain. Brute force over short printable keys: with max_size buckets the
// expected number of tries is max_size, which is microseconds at this size.
//
// This exists because the miss-is-expensive claim is only worth making if it
// can be shown rather than asserted, and every key already in the interesting
// bucket is the same string.
bool findCollision(unsigned long bucket, int maxSize, std::string &out) {
    char buf[16];
    for (long n = 0; n < 40L * maxSize; n++) {
        const int len = std::snprintf(buf, sizeof buf, "zz%ld", n);
        if (len <= 0) return false;
        const std::string cand(buf, (std::size_t)len);
        if (djb2(cand) % (unsigned long)maxSize == bucket) {
            out = cand;
            return true;
        }
    }
    return false;
}

void cmdDemo(MyDS &ds) {
    ds = MyDS();

    std::cout <<
        "\n"
        "  ── 1. A fresh table ─────────────────────────────────────────────\n"
        "\n"
        "  The constructor does `max_size = 60000; nodes.resize(max_size);` with\n"
        "  the comment \"Initialized at 60000 to avoid expensive rehashing at\n"
        "  larger sizes\". That is 60,000 empty std::list objects, about 1.5 MB,\n"
        "  allocated before a single key exists.\n"
        "\n";
    cmdStats(ds);

    std::cout <<
        "  ── 2. A real dataset ────────────────────────────────────────────\n"
        "\n"
        "  4,488 English words. Note that `size` and the counted number of\n"
        "  occupied buckets agree exactly — they always do, until a rehash.\n"
        "\n";
    cmdLoad(ds, "common");
    cmdStats(ds);

    const Stats before = measure(ds);
    std::cout <<
        "  Collisions are already here: " << before.elements << " words occupy only "
        << before.occupied << "\n  buckets, so " << (before.elements - before.occupied)
        << " of them are sharing with somebody. Remember that number.\n"
        "\n"
        "  ── 3. Why this table will not grow on its own ───────────────────\n"
        "\n"
        "  push() grows when EITHER trigger fires:\n"
        "\n"
        "      if (size == nodes.size() || hashList.size() >= 150)\n"
        "\n"
        "  The first needs 60,000 OCCUPIED BUCKETS — more distinct buckets in use\n"
        "  than this dataset has words. It cannot fire. So the only reachable\n"
        "  trigger is a single chain reaching 150, and because push() does not\n"
        "  deduplicate, the way to get there is to insert one word 150 times.\n"
        "\n"
        "  ── 4. Inserting \"seminole\" 150 more times ───────────────────────\n"
        "\n";

    int rehashes = 0;
    int prevMax = ds.max_size;
    for (int i = 0; i < 150; i++) {
        ds.push("seminole");
        if (ds.max_size != prevMax) {
            std::cout << "    insert #" << (i + 1) << ": max_size " << prevMax
                      << " -> " << ds.max_size << "\n";
            prevMax = ds.max_size;
            rehashes++;
        }
    }
    std::cout << "\n  " << rehashes << (rehashes == 1 ? " rehash.\n" : " rehashes.\n");
    if (rehashes > 1) {
        std::cout <<
            "\n"
            "  More than one, and that is the bug rather than the demo being\n"
            "  greedy. The trigger is a CHAIN LENGTH, but the chain is 150 copies\n"
            "  of the same string — so rehashing cannot split it, the condition is\n"
            "  still true on the next insert, and the table quadruples again.\n"
            "  Every subsequent push of that word rebuilds the entire structure.\n"
            "  Left alone, max_size *= 4 overflows int after a few more.\n";
    }
    std::cout << "\n";

    std::cout <<
        "  ── 5. What the rehash quietly broke ─────────────────────────────\n\n";
    cmdStats(ds);
    const Stats after = measure(ds);
    std::cout <<
        "  Those " << (before.elements - before.occupied) << " colliding words got"
        " spread across the bigger table, so the real\n"
        "  occupied count rose from " << before.occupied << " to " << after.occupied
        << " — a jump of " << (after.occupied - before.occupied) << ". `size` rose by\n"
        "  " << ((long)ds.size - (long)before.occupied)
        << ", for the one genuinely new bucket \"seminole\" created, because push()\n"
        "  only touches it on an empty -> non-empty transition and rehash() never\n"
        "  recomputes it. The class's own idea of how full it is is now stale,\n"
        "  permanently — and it is the left-hand side of the growth condition.\n"
        "\n";

    const unsigned long hotBucket = djb2("seminole") % (unsigned long)ds.max_size;
    const long chainLen = (long)ds.nodes[hotBucket].size();

    std::cout <<
        "  ── 6. The best part ─────────────────────────────────────────────\n"
        "\n"
        "  One bucket now holds " << chainLen << " copies of the same string, and"
        " search() is\n"
        "  std::find over that std::list:\n"
        "\n";
    cmdSearch(ds, "seminole");

    std::string collider;
    if (findCollision(hotBucket, ds.max_size, collider)) {
        std::cout <<
            "  Now a key that is NOT in the table but hashes to that same bucket\n"
            "  (found by brute force just now, so the comparison is real):\n"
            "\n";
        cmdSearch(ds, collider);
        std::cout <<
            "  A hit stops at the first node. A miss walks all " << chainLen << "."
            " In this table an\n"
            "  unsuccessful lookup is " << chainLen << "x the cost of a successful"
            " one, which is the\n"
            "  opposite of what a hash table is for — and it follows directly from\n"
            "  push() not deduplicating.\n";
    }
    std::cout <<
        "\n"
        "  Try `chain seminole`, or `warnings`, or `reset` and start over.\n"
        "\n";
}

bool dispatch(MyDS &ds, const std::string &line) {
    std::istringstream iss(line);
    std::string cmd;
    if (!(iss >> cmd)) return true;

    std::string arg;
    std::getline(iss, arg);
    // Trim the single leading space getline leaves behind; keep the rest, so a
    // key may contain spaces.
    if (!arg.empty() && arg[0] == ' ') arg.erase(0, 1);

    if (cmd == "quit" || cmd == "exit") return false;
    if (cmd == "help" || cmd == "?")    { printHelp(); return true; }
    if (cmd == "stats")                 { cmdStats(ds); return true; }
    if (cmd == "warnings")              { cmdWarnings(); return true; }
    if (cmd == "demo")                  { cmdDemo(ds); return true; }
    if (cmd == "reset") {
        ds = MyDS();
        std::cout << "  fresh MyDS: 60000 buckets, 0 elements\n\n";
        return true;
    }
    if (arg.empty()) {
        std::cout << "  " << cmd << " needs an argument. `help` for the list.\n\n";
        return true;
    }
    if (cmd == "insert" || cmd == "push") { cmdInsert(ds, arg); return true; }
    if (cmd == "search" || cmd == "find") { cmdSearch(ds, arg); return true; }
    if (cmd == "chain")                   { cmdChain(ds, arg);  return true; }
    if (cmd == "hash")                    { cmdHash(ds, arg);   return true; }
    if (cmd == "load")                    { cmdLoad(ds, arg);   return true; }

    std::cout << "  unknown command: " << cmd << " — `help` for the list.\n\n";
    return true;
}

}  // namespace

int main() {
    MyDS ds;
    printBanner();

    for (;;) {
        // Flush explicitly. The prompt has no trailing newline, and while a PTY
        // is line-buffered, this also has to behave when stdout is a pipe —
        // `docker run -i`, a CI check — where it would otherwise sit in a 4 KB
        // buffer and appear only at exit.
        std::cout << "myds> " << std::flush;

        std::string line;
        // Test the STREAM, not the string. getline sets failbit at EOF, and
        // that is the difference between exiting cleanly when ttyd closes the
        // PTY master and spinning forever on an empty line. Cloysta, two ports
        // over, is the cautionary tale: REPL/read.c tests a pointer that can
        // never be null and so never sees EOF at all.
        if (!std::getline(std::cin, line)) {
            std::cout << "\n";
            break;
        }
        if (!dispatch(ds, line)) break;
    }
    return 0;
}
