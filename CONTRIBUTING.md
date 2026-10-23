# Brindle Contributor License Agreement 1.0

This agreement is between you and **PawDelta LLC**, a New York limited liability company ("PawDelta"). It covers every contribution you submit to Brindle. To sign it, comment on your pull request exactly as the CLA bot asks. Your signature, GitHub account and the date are recorded.

## 1. Definitions

**You** means the individual who signs this agreement. If you contribute on behalf of an employer or another entity, it also means that entity, and you confirm you are authorized to sign for it.

A **contribution** is any code, documentation or other material you submit to PawDelta for Brindle, through its repository, a pull request, an issue, or any other channel PawDelta designates, unless you mark it in writing as "Not a Contribution" when you submit it.

## 2. Copyright License

You grant PawDelta and anyone who receives software from PawDelta a perpetual, worldwide, non-exclusive, royalty-free, irrevocable license to reproduce, prepare derivative works of, publicly display, publicly perform, sublicense and distribute your contributions and their derivative works, under any terms PawDelta chooses, including proprietary and commercial terms.

## 3. Patent License

You grant PawDelta and anyone who receives software from PawDelta a perpetual, worldwide, non-exclusive, royalty-free, irrevocable license to make, have made, use, sell, offer for sale, import and otherwise transfer your contributions, alone or combined with Brindle, under any patent claims you can license that your contributions, alone or combined with Brindle, would infringe.

## 4. Your Statements

You state that:

1. each contribution is your original work, or you have the right to submit it under this agreement, and you have described in your submission any third-party material and its license;
2. if your employer or another party has rights in your contributions, that party has allowed you to make them under this agreement, or has waived those rights; and
3. you will tell PawDelta promptly if any of these statements stops being true.

## 5. No Obligation

PawDelta does not have to use your contributions. Your contributions give you no further rights to Brindle, which you use under its own license.

## 6. Moral Rights

To the extent permitted by law, you waive, and agree not to assert against PawDelta or its licensees, any moral rights in your contributions.

## 7. As Is

Unless required by law, you provide your contributions as is, without warranties or conditions of any kind, other than the statements in section 4.

## 8. Governing Law

This agreement is governed by the laws of the State of New York, without regard to its conflict-of-laws rules.

---

## The desktop app (`app/`)

The desktop app lives in `app/` beside the engine, and this agreement covers
contributions to it like any other. It needs Node 22 and stable Rust:

```sh
npm --prefix app ci
npm --prefix app test -- --run
cargo test --manifest-path app/src-tauri/Cargo.toml
```

The app talks to the engine in this same checkout: `npm --prefix app run
gen:api` regenerates `app/src/api/schema.ts` from
`src/brindle/daemon/api-schema.json`, and `app/scripts/build_engine.sh` bundles
the engine from this checkout's `HEAD`. `app/README.md` has the rest.
