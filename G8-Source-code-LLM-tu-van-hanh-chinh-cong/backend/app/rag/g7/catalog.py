"""Reviewed routing metadata, never a source of administrative requirements.

No utterance lookup table: descriptions explain the purpose and boundary of each
catalog entry. IDs/titles/available fields always come from the approved corpus.
"""
from app.rag.catalog_policy import is_serving_candidate

DOMAINS = {
    'civil': 'Hộ tịch', 'marriage': 'Hôn nhân', 'birth': 'Khai sinh, quan hệ cha mẹ con',
    'death': 'Khai tử, hỗ trợ khi qua đời', 'housing': 'Nhà ở', 'land': 'Đất đai',
    'construction': 'Xây dựng', 'business': 'Hộ kinh doanh', 'employment': 'Lao động, việc làm',
    'education': 'Giáo dục', 'welfare': 'Bảo trợ xã hội', 'merit': 'Người có công',
    'certification': 'Chứng thực',
}
LABELS = {
    '011f005278ef': 'register_labor_rules', '10ed2a9ea4a7': 'cremation_support',
    '1620179477b4': 'marriage_foreign_element', '1e0d3df69ad5': 'certification',
    '2dc6a6a0b92a': 'secondary_school_transfer', '30228658fce3': 'confirm_housing_status',
    '34afa9c1ded0': 'business_temporary_suspension', '3cc26cfe53d5': 'disability_assessment',
    '54af5c03413a': 'replace_merit_certificate', '55245834a3c0': 'house_number_assignment',
    '56784de1011e': 'education_support', '58aa31837615': 'birth_registration',
    '61e34e3360a8': 'employment_loan_business', '654f41c7e224': 'funeral_social_pension',
    '6696b9fb226a': 'death_registration', '69915286e651': 'confirm_house_land_location',
    '6b05578da38d': 'resistance_participant_benefit', '6d6862db57ba': 'marriage_domestic',
    '72687979d6ad': 'planning_information', '7677bda9d6bd': 'martyr_worship_allowance',
    '829daa0c9629': 'business_permanent_closure', '85fb93f4cc41': 'social_pension',
    '86cb084c633d': 'reissue_business_certificate', 'aa19a2014e41': 'merit_relative_certificate',
    'b1224d6b5835': 'monthly_social_assistance', 'ba78b5d39212': 'confirm_marital_status',
    'c343c85df1af': 'first_land_registration', 'c98b4aa39036': 'construction_permit',
    'e5945d1751b7': 'create_household_business', 'eb5f9df122cd': 'notify_construction_start',
    'f29de859e25e': 'funeral_merit_recipient', 'f85a8600ee33': 'change_business_registration',
    'f8c56de9462a': 'employment_loan_individual', 'fa144e522190': 'funeral_social_assistance',
    'ff5c71001a91': 'policy_scholarship', 'ffba4ebaacd0': 'recognize_parent_child',
}
# Contrastive availability metadata, not document evidence or utterance rules.
# These needs are deliberately NOT in the serving corpus. Giving them explicit
# labels avoids forcing a nearby supported service for an unsupported operation.
SUPPORT_BOUNDARIES = {
    'unsupported_conceal_harm': 'Yêu cầu che giấu giết người, ngụy tạo nguyên nhân tử vong, làm giả giấy tờ để né điều tra; không áp cho khai tử thông thường.',
    'unsupported_land_transfer': 'Sang tên/chuyển quyền do mua bán, chuyển nhượng, tặng cho, thừa kế đất đã có chủ; khác đăng ký đất đai lần đầu.',
    'unsupported_reissue_land_title': 'Cấp lại hoặc đổi giấy chứng nhận quyền sử dụng đất, sổ đỏ, sổ hồng đã có; khác đăng ký lần đầu và số nhà.',
    'unsupported_immigration_visa': 'Xin thị thực/visa, xuất nhập cảnh, hộ chiếu; không có trong nguồn thủ tục đang phục vụ.',
    'unsupported_cooking': 'Công thức, cách nấu ăn, làm bánh, món ăn; không phải thủ tục đăng ký hộ kinh doanh.',
}
# Subject tags are curated, not inferred from the word "nhà" or "việc".
DESCRIPTIONS = {
    '011f005278ef': ('employment', 'Đăng ký nội quy lao động của doanh nghiệp; không phải vay vốn.'),
    '10ed2a9ea4a7': ('death welfare', 'Hỗ trợ chi phí hỏa táng; phân biệt khai tử và trợ cấp mai táng.'),
    '1620179477b4': ('civil marriage', 'Kết hôn có yếu tố nước ngoài; quốc tịch khác không đồng nghĩa nộp tại nước ngoài.'),
    '1e0d3df69ad5': ('certification', 'Chứng thực chữ ký, hợp đồng, giao dịch, di chúc; không đồng nghĩa mọi loại công chứng.'),
    '2dc6a6a0b92a': ('education', 'Chuyển trường cấp THCS; không dùng cho mọi cấp học.'),
    '30228658fce3': ('housing', 'Xin xác nhận tình trạng nhà ở; khác tình trạng hôn nhân, vị trí nhà và cấp số nhà.'),
    '34afa9c1ded0': ('business', 'Hộ kinh doanh tạm nghỉ rồi bán lại hoặc hoạt động lại trước hạn; không đóng cửa vĩnh viễn.'),
    '3cc26cfe53d5': ('welfare', 'Xác định mức độ khuyết tật, giấy xác nhận khuyết tật; khác xin trợ cấp.'),
    '54af5c03413a': ('merit', 'Đổi bằng Tổ quốc ghi công; không phải xin trợ cấp thờ cúng.'),
    '55245834a3c0': ('housing', 'Cấp số địa chỉ căn nhà. SỐ NHÀ khác SỔ NHÀ; sổ đỏ, sổ hồng không phải số nhà.'),
    '56784de1011e': ('education merit', 'Chế độ hỗ trợ theo học đến đại học cho đối tượng chính sách; khác học bổng.'),
    '58aa31837615': ('civil birth', 'Đăng ký khai sinh cho người mới sinh, làm giấy khai sinh; không phải xác nhận quan hệ cha con.'),
    '61e34e3360a8': ('employment business', 'Vay vốn tạo việc làm cho cơ sở sản xuất kinh doanh; phân biệt người lao động vay cá nhân.'),
    '654f41c7e224': ('death welfare', 'Hỗ trợ mai táng khi người hưởng trợ cấp hưu trí xã hội qua đời; cần đúng đối tượng.'),
    '6696b9fb226a': ('civil death', 'Đăng ký khai tử khi có người qua đời, mất, chết; không phải cấp lại giấy tờ bị mất.'),
    '69915286e651': ('housing land', 'Xác nhận vị trí nhà, đất; khác tình trạng nhà ở và đăng ký quyền đất.'),
    '6b05578da38d': ('merit', 'Chế độ người hoạt động kháng chiến, bảo vệ tổ quốc, nghĩa vụ quốc tế.'),
    '6d6862db57ba': ('civil marriage', 'Đăng ký quan hệ vợ chồng, kết hôn trong nước; khác xin giấy xác nhận độc thân.'),
    '72687979d6ad': ('land housing construction', 'Tra cứu, cung cấp thông tin quy hoạch; khác giấy phép xây dựng.'),
    '7677bda9d6bd': ('merit death', 'Trợ cấp thờ cúng liệt sĩ; chỉ quan hệ thân nhân chưa đủ xác định nhu cầu thờ cúng.'),
    '829daa0c9629': ('business', 'Chấm dứt hộ kinh doanh, nghỉ hẳn; không phải tạm đóng quán rồi mở lại.'),
    '85fb93f4cc41': ('welfare', 'Thực hiện, điều chỉnh, thôi trợ cấp hưu trí xã hội; khác lương hưu bảo hiểm.'),
    '86cb084c633d': ('business', 'Cấp lại giấy chứng nhận hộ kinh doanh bị mất hoặc hỏng; không phải đăng ký mới.'),
    'aa19a2014e41': ('merit', 'Giấy xác nhận thân nhân người có công; không đồng nghĩa một khoản trợ cấp cụ thể.'),
    'b1224d6b5835': ('welfare', 'Trợ cấp xã hội, hỗ trợ chăm sóc nuôi dưỡng hàng tháng; phân biệt hưu trí xã hội.'),
    'ba78b5d39212': ('civil marriage', 'Giấy xác nhận tình trạng hôn nhân, độc thân; không phải đăng ký kết hôn.'),
    'c343c85df1af': ('land housing', 'Đăng ký đất đai, tài sản gắn với đất lần đầu; không phải sang tên, chuyển nhượng hay cấp lại sổ.'),
    'c98b4aa39036': ('construction housing', 'Xin cấp giấy phép xây dựng; khác thông báo khởi công và quy hoạch.'),
    'e5945d1751b7': ('business', 'Đăng ký thành lập hộ kinh doanh mới; không phải lập công ty.'),
    'eb5f9df122cd': ('construction housing', 'Thông báo khởi công xây dựng; khác xin giấy phép xây dựng.'),
    'f29de859e25e': ('merit death', 'Trợ cấp khi người có công đang hưởng trợ cấp ưu đãi từ trần; cần đúng đối tượng.'),
    'f85a8600ee33': ('business', 'Thay đổi nội dung đăng ký hộ kinh doanh đang có; không phải cấp lại vì mất giấy.'),
    'f8c56de9462a': ('employment', 'Người lao động vay vốn tạo, duy trì, mở rộng việc làm; không vay cưới hỏi.'),
    'fa144e522190': ('death welfare', 'Hỗ trợ mai táng cho đối tượng bảo trợ xã hội; khác hưu trí xã hội và khai tử.'),
    'ff5c71001a91': ('education', 'Xét cấp học bổng chính sách; không phải miễn giảm học phí. Một số nguồn hồ sơ đang có xung đột.'),
    'ffba4ebaacd0': ('civil birth', 'Đăng ký nhận cha mẹ con, xác lập quan hệ huyết thống; chỉ nói sinh con chưa phải yêu cầu này.'),
}


def cards(data):
    result = []
    for pid in sorted(data.accepted):
        if not is_serving_candidate(pid):
            continue
        tags, purpose = DESCRIPTIONS.get(pid.removeprefix('d2_title_'), ('', ''))
        if not purpose:
            raise ValueError('G7_CATALOG_METADATA_MISSING:' + pid)
        row = data.procedures[pid]
        result.append({'code': f'T{len(result)+1:02}', 'id': pid, 'title': row['title'],
                       'label': LABELS[pid.removeprefix('d2_title_')],
                       'domains': tags.split(), 'purpose': purpose})
    return result


def matching_cards(catalog, domains):
    return [c for c in catalog if set(c['domains']) & set(domains)]
