    results = st.session_state.get("bulk_results", [])
    if results:
        st.divider()
        st.markdown("### 📊 일괄 분석 결과")

        rows = []
        for result in results:
            c = result["company"]
            r = result.get("report") or {}
            banks = result.get("banks", [])
            rows.append({
                "법인명": c.get("법인명", ""),
                "대표자": c.get("대표자", ""),
                "주소": c.get("주소", ""),
                "최근감사보고서": r.get("보고서명", ""),
                "접수일": r.get("접수일", ""),
                "확인은행": ", ".join(b["은행"] for b in banks),
                "확인은행수": len(banks),
                "퇴직연금":(result.get("pension") or {}).get("운영분류", "확인 불가"),
                "비고": result.get("error", ""),
            })

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        excel = make_excel(results)
        st.download_button(
            "📥 전체 결과 엑셀 다운로드",
            data=excel,
            file_name="법인Finder_일괄분석.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


with db_tab:
    st.subheader("DART 본점주소 DB 구축 / 백업")
    st.markdown("#### 💾 전체 DB 백업 / 복원 (사내망 PC 이동용)")
    st.caption("법인목록·주소·전화번호·기존 거래은행/퇴직연금 분석 결과를 한 파일로 보관합니다. DB 복원 시 현재 데이터를 대체합니다.")
    st.download_button(
        "📥 전체 DB(SQLite) 백업 다운로드",
        data=export_full_db_bytes(),
        file_name="corp_finder_cache.sqlite3",
        mime="application/x-sqlite3",
        key="full_db_export",
    )
    full_restore_file = st.file_uploader("전체 DB 백업파일 복원", type=["sqlite3", "db"], key="full_db_restore_file")
    if full_restore_file is not None and st.button("전체 DB 복원 실행", key="full_db_restore_btn"):
        try:
            restore_full_db_bytes(full_restore_file.getvalue())
            _db_cloud_push()
            st.success("전체 DB 복원 완료. 법인 목록을 다시 불러옵니다.")
            st.rerun()
        except Exception as exc:
            st.error(f"전체 DB 복원 실패: {exc}")
    st.divider()
    total_cnt, addr_cnt, fail_cnt = address_db_stats()
    cols = st.columns(4)
    cols[0].metric("DART 법인", f"{total_cnt:,}")
    cols[1].metric("주소 수집 완료", f"{addr_cnt:,}")
    cols[2].metric("수집 오류", f"{fail_cnt:,}")
    pct = (addr_cnt / total_cnt * 100) if total_cnt else 0
    cols[3].metric("진행률", f"{pct:.1f}%")

    st.info(
        "DART 고유번호 목록에는 주소가 없어서 기업개황 API를 회사별로 조회해야 합니다. "
        "따라서 전체 주소 DB는 한 번에 만들기보다 아래 배치 단위로 누적하는 방식입니다. "
        "OpenDART 호출량 제한에 걸릴 수 있으므로 배치 구축이 안전합니다. "
        "법인명 조회/감사보고서 분석을 한 회사는 자동으로 주소 DB에도 저장됩니다."
    )

    b1, b2, b3 = st.columns([1, 1, 1])
    batch_size = b1.selectbox("이번에 수집할 회사 수", [20, 50, 100, 200, 300, 500, 1000], index=2, key="db_batch_size")
    listed_first = b2.checkbox("상장사 우선", value=True, key="db_listed_first")
    retry_errors = b3.checkbox("오류 회사 재시도 포함", value=False, key="db_retry_errors")

    if st.button("주소 DB 이어서 구축", type="primary", key="db_build_btn", disabled=not dart_enabled):
        ok, fail = build_address_cache(api_key, batch_size, listed_first, retry_errors)
        if ok == 0 and fail == 0:
            st.success("추가로 수집할 회사가 없습니다.")
        else:
            st.success(f"이번 실행: 성공 {ok:,}개 / 오류 {fail:,}개")
            st.rerun()

    st.markdown("#### 주소 DB 백업 / 복원")
    st.caption("엑셀은 주소 정보만 백업합니다. 사내망 PC 이동에는 위의 전체 DB(SQLite) 백업을 사용하세요. Streamlit Cloud 로컬 디스크는 영구 보관을 보장하지 않습니다.")

    st.download_button(
        "📥 주소 DB 엑셀 백업",
        data=export_address_cache_excel(),
        file_name="법인Finder_주소DB.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="db_export_btn",
    )

    restore_file = st.file_uploader("주소 DB 백업파일 복원", type=["xlsx", "xls", "csv"], key="db_restore_file")
    if restore_file is not None and st.button("주소 DB 복원 실행", key="db_restore_btn"):
        try:
            count = import_address_cache_file(restore_file)
            _db_cloud_push()
            st.success(f"주소 DB {count:,}건을 복원했습니다.")
            st.rerun()
        except Exception as e:
            st.error(f"복원 실패: {e}")


st.divider()
st.caption(
    "법인 Finder는 DART 및 사용자가 불러온 공공/업무용 기업목록을 결합해 법인 탐색과 DART 공시 분석을 돕는 업무보조 도구입니다. "
    "특정 은행명이 기재되어 있다는 사실만으로 해당 은행을 주거래은행이라고 단정할 수 없으며, "
    "최종 판단은 감사보고서 원문과 회사 확인을 통해 검증해야 합니다."
)
