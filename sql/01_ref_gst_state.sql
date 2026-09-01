-- ref_gst_state: BC state code -> the "NN-NAME" label the Sales Register
-- GST Detail report prints, plus the numeric GST state code.
--
-- BC has no published web service for its State table, so this is a static
-- reference. The 20 rows marked verified=true were derived by pairing every
-- April-2026 document in bc_posted_sales_invoice_excel /
-- bc_posted_sales_credit_memo with the same document in the report export
-- (Sales Register GST Detail.xlsx) - each BC code mapped to exactly one
-- printed label, no ambiguity. Two of them are NOT what you would guess:
-- Andhra Pradesh is 'AD' (not AP) and Odisha is 'OD' (not OR).
--
-- The remaining rows use the statutory GST code list with the conventional
-- two-letter code. They are marked verified=false because no April-2026
-- document used them; check one against BC before relying on it. The view
-- falls back to the raw BC code when a code is missing here, so an unmapped
-- state shows up as itself rather than as NULL.
CREATE TABLE IF NOT EXISTS public.ref_gst_state (
    state_code   text PRIMARY KEY,     -- BC State.Code, e.g. 'MH'
    gst_state_no text NOT NULL,        -- statutory 2-digit code, e.g. '27'
    state_name   text NOT NULL,        -- e.g. 'MAHARASHTRA'
    label        text GENERATED ALWAYS AS (gst_state_no || '-' || state_name) STORED,
    verified     boolean NOT NULL DEFAULT false
);

INSERT INTO public.ref_gst_state (state_code, gst_state_no, state_name, verified) VALUES
    ('JK','01','JAMMU AND KASHMIR',                        false),
    ('HP','02','HIMACHAL PRADESH',                         false),
    ('PB','03','PUNJAB',                                   true),
    ('CH','04','CHANDIGARH',                               false),
    ('UK','05','UTTARAKHAND',                              true),
    ('HR','06','HARYANA',                                  true),
    ('DL','07','DELHI',                                    true),
    ('RJ','08','RAJASTHAN',                                true),
    ('UP','09','UTTAR PRADESH',                            true),
    ('BR','10','BIHAR',                                    true),
    ('SK','11','SIKKIM',                                   false),
    ('AR','12','ARUNACHAL PRADESH',                        false),
    ('NL','13','NAGALAND',                                 false),
    ('MN','14','MANIPUR',                                  false),
    ('MZ','15','MIZORAM',                                  false),
    ('TR','16','TRIPURA',                                  false),
    ('ML','17','MEGHALAYA',                                false),
    ('AS','18','ASSAM',                                    true),
    ('WB','19','WEST BENGAL',                              true),
    ('JH','20','JHARKHAND',                                true),
    ('OD','21','ODISHA',                                   true),
    ('CG','22','CHATTISGARH',                              false),
    ('MP','23','MADHYA PRADESH',                           true),
    ('GJ','24','GUJARAT',                                  true),
    ('DN','26','DADRA AND NAGAR HAVELI AND DAMAN AND DIU', false),
    ('MH','27','MAHARASHTRA',                              true),
    ('KA','29','KARNATAKA',                                true),
    ('GA','30','GOA',                                      true),
    ('LD','31','LAKSHADWEEP',                              false),
    ('KL','32','KERALA',                                   true),
    ('TN','33','TAMIL NADU',                               true),
    ('PY','34','PUDUCHERRY',                               false),
    ('AN','35','ANDAMAN AND NICOBAR ISLANDS',              false),
    ('TS','36','TELANGANA',                                true),
    ('AD','37','ANDHRA PRADESH',                           true),
    ('LA','38','LADAKH',                                   false),
    ('OT','97','OTHER TERRITORY',                          false)
ON CONFLICT (state_code) DO UPDATE
    SET gst_state_no = EXCLUDED.gst_state_no,
        state_name   = EXCLUDED.state_name,
        verified     = EXCLUDED.verified;

ALTER TABLE public.ref_gst_state ENABLE ROW LEVEL SECURITY;
